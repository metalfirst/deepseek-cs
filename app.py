import os
import json
import uuid
import time
import hashlib
import base64
import struct
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from Crypto.Cipher import AES
import requests

app = Flask(__name__)
CORS(app)

# ==================== 日志配置 ====================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ==================== 配置（从环境变量读取，请务必设置） ====================
# DeepSeek 配置
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")          # 你的 DeepSeek API Key
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"

# 企业微信主动发送消息配置
WECOM_CORP_ID = "ww2e3b75e9697e5c62"                           # 你的企业ID
WECOM_AGENT_ID = 1000005                                        # 你的应用AgentId
WECOM_SECRET = os.environ.get("WECOM_SECRET")                   # 应用Secret
CUSTOMER_SERVICE_USERID = os.environ.get("CUSTOMER_SERVICE_USERID", "YangJun")  # 接收通知的客服账号

# 企业微信回调配置（用于接收消息，如果不需接收可留空）
WECOM_TOKEN = os.environ.get("WECOM_TOKEN")                     # 自定义Token，与后台一致
WECOM_ENCODING_AES_KEY = os.environ.get("WECOM_ENCODING_AES_KEY")  # 43位EncodingAESKey

# 会话超时（秒）
ACTIVITY_TIMEOUT = 5 * 60                                       # 5分钟

# ==================== 知识库加载 ====================
KNOWLEDGE_FILE = "knowledge.txt"
knowledge_text = ""
if os.path.exists(KNOWLEDGE_FILE):
    try:
        with open(KNOWLEDGE_FILE, "r", encoding="utf-8") as f:
            knowledge_text = f.read()
        logger.info("知识库加载成功，长度: %d", len(knowledge_text))
    except Exception as e:
        logger.error("读取知识库失败: %s", e)
else:
    logger.warning("未找到知识库文件 %s，将不使用额外知识库。", KNOWLEDGE_FILE)

# ==================== 无关话题过滤 ====================
OFF_TOPIC_KEYWORDS = [
    "天气", "weather", "股票", "stock", "政治", "politics", "新闻", "news",
    "游戏", "game", "娱乐", "entertainment", "电影", "movie", "音乐", "music",
    "赌博", "gambling", "色情", "porn", "暴力", "violence", "战争", "war",
    "比特币", "bitcoin", "加密货币", "crypto", "明星", "celebrity"
]

def is_out_of_scope(message):
    msg_lower = message.lower()
    for kw in OFF_TOPIC_KEYWORDS:
        if kw in msg_lower:
            return True
    return False

# ==================== 系统提示词 ====================
SYSTEM_PROMPT = (
    "你是上海巨红贸易有限公司（Unionmetal Trading）的AI客服助手。\n"
    "公司主营钢材出口：钢卷（热轧、冷轧、不锈钢）、钢管（无缝、焊接、方矩管）、型钢（角钢、槽钢、工字钢、H型钢）。\n"
    "你的职责仅限于回答关于钢材产品、规格、标准、采购、物流、付款等业务相关的问题。\n"
    "如果用户询问与公司业务完全无关的问题（如天气、股票、娱乐等），请礼貌地拒绝，并引导用户提出钢材相关的问题。\n"
    "示例回复：'抱歉，我们只提供钢材相关咨询服务。请问您需要了解哪种钢材产品？'\n"
    "其他任务：\n"
    "1. 用客户使用的语言（中文、英语、阿拉伯语等）回复。\n"
    "2. 回答关于产品规格、标准、最小起订量、交货期、付款方式等专业问题。\n"
    "3. 如果客户询问价格，引导其提供具体需求（产品、规格、数量、目的港）并告知将转销售跟进。\n"
    "4. 当客户表示需要人工帮助时（如'转人工'、'human'、'بشري'），记录需求并告知将通知人工客服。\n"
    "5. 保持专业、友好、简洁的语调。"
)

# ==================== 会话管理（内存存储） ====================
memory_store = {}          # {session_id: {"history": list, "last_active": timestamp}}

def get_session_history(session_id):
    """获取会话历史，若不存在则初始化"""
    if session_id not in memory_store:
        history = [{"role": "system", "content": SYSTEM_PROMPT}]
        memory_store[session_id] = {"history": history, "last_active": time.time()}
        return history
    return memory_store[session_id]["history"]

def save_session_history(session_id, history):
    """保存会话历史并更新最后活动时间"""
    memory_store[session_id] = {"history": history, "last_active": time.time()}

def check_activity(session_id):
    """检查会话是否超时，超时则删除并返回 True"""
    if session_id in memory_store:
        last = memory_store[session_id]["last_active"]
        if time.time() - last > ACTIVITY_TIMEOUT:
            del memory_store[session_id]
            return True
    return False

def update_activity(session_id):
    """更新会话的最后活动时间"""
    if session_id in memory_store:
        memory_store[session_id]["last_active"] = time.time()

def trim_history(history, max_turns=10):
    """保留系统消息 + 最近 max_turns 轮对话"""
    if len(history) <= max_turns * 2 + 1:
        return history
    return [history[0]] + history[-(max_turns * 2):]

# ==================== 知识库检索（简单关键词匹配） ====================
def retrieve_knowledge(query):
    if not knowledge_text:
        return ""
    keywords = [w for w in query.lower().split() if len(w) > 1]
    if not keywords:
        return ""
    lines = knowledge_text.split('\n')
    matched = []
    for line in lines:
        line_lower = line.lower()
        if any(k in line_lower for k in keywords):
            matched.append(line)
    return "\n".join(matched[:3])

# ==================== 多语言转人工检测 ====================
def is_human_request(message):
    cn_keywords = ["转人工", "人工客服", "人工服务", "找人工", "人工", "真人", "找客服"]
    en_keywords = [
        "human", "agent", "speak to human", "talk to human",
        "customer service", "support", "real person", "live agent",
        "transfer to human", "human agent"
    ]
    ar_keywords = [
        "بشري", "دعم", "خدمة العملاء", "وكيل", "التحدث إلى بشري",
        "تحويل إلى وكيل", "مساعد بشري"
    ]
    all_keywords = cn_keywords + en_keywords + ar_keywords
    msg_lower = message.lower()
    return any(k.lower() in msg_lower for k in all_keywords)

# ==================== 企业微信主动发送消息 ====================
wecom_token_cache = {"token": None, "expire_time": 0}

def get_wecom_access_token():
    now = time.time()
    if wecom_token_cache["token"] and now < wecom_token_cache["expire_time"]:
        return wecom_token_cache["token"]
    url = f"https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid={WECOM_CORP_ID}&corpsecret={WECOM_SECRET}"
    try:
        resp = requests.get(url, timeout=10).json()
        if resp.get("errcode") == 0:
            token = resp["access_token"]
            expires_in = resp["expires_in"]
            wecom_token_cache["token"] = token
            wecom_token_cache["expire_time"] = now + expires_in - 300
            return token
        else:
            logger.error("获取 access_token 失败: %s", resp)
    except Exception as e:
        logger.error("请求 access_token 异常: %s", e)
    return None

def send_to_wecom(userid, content):
    """发送消息给企业微信成员"""
    token = get_wecom_access_token()
    if not token:
        return False
    url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
    data = {
        "touser": userid,
        "msgtype": "text",
        "agentid": WECOM_AGENT_ID,
        "text": {"content": content},
        "safe": 0
    }
    try:
        resp = requests.post(url, json=data, timeout=10).json()
        if resp.get("errcode") == 0:
            return True
        else:
            logger.error("发送消息失败: %s", resp)
            return False
    except Exception as e:
        logger.error("发送消息异常: %s", e)
        return False

# ==================== 企业微信回调加解密 ====================
def decrypt_wecom_msg(encrypt_msg, msg_signature, timestamp, nonce):
    """解密企业微信推送的消息（或 echostr），返回明文"""
    if not WECOM_TOKEN or not WECOM_ENCODING_AES_KEY:
        logger.warning("回调配置缺失，无法解密")
        return None

    # 1. 验证签名
    arr = [WECOM_TOKEN, timestamp, nonce, encrypt_msg]
    arr.sort()
    tmp_str = "".join(arr)
    computed = hashlib.sha1(tmp_str.encode()).hexdigest()
    if computed != msg_signature:
        logger.warning("签名验证失败")
        return None

    # 2. 解密
    try:
        aes_key = base64.b64decode(WECOM_ENCODING_AES_KEY + "=")
        cipher = AES.new(aes_key, AES.MODE_CBC, aes_key[:16])
        decrypted = cipher.decrypt(base64.b64decode(encrypt_msg))

        # 去除补位字符
        pad = decrypted[-1]
        content = decrypted[16:-pad]

        # 前4字节为长度
        xml_len = struct.unpack("!I", content[:4])[0]
        plain_text = content[4:4+xml_len].decode('utf-8')
        return plain_text
    except Exception as e:
        logger.error("解密失败: %s", e)
        return None

def parse_wecom_xml(xml_str):
    """解析 XML 消息结构体，返回字典"""
    try:
        root = ET.fromstring(xml_str)
        result = {}
        for child in root:
            result[child.tag] = child.text
        return result
    except Exception as e:
        logger.error("解析 XML 失败: %s", e)
        return None

# ==================== Flask 路由 ====================
@app.route('/')
def index():
    """渲染网页聊天界面"""
    return render_template('chat.html')

@app.route('/api/chat', methods=['POST'])
def chat():
    """网页端聊天接口"""
    data = request.json
    user_message = data.get('message', '').strip()
    session_id = data.get('session_id', '')

    if not user_message:
        return jsonify({'error': '消息不能为空'}), 400

    # 会话管理
    if not session_id:
        session_id = str(uuid.uuid4())
    else:
        if check_activity(session_id):
            new_session_id = str(uuid.uuid4())
            reply = "由于长时间未活动，会话已结束。您可以重新开始对话。"
            update_activity(new_session_id)
            return jsonify({'reply': reply, 'session_id': new_session_id})

    update_activity(session_id)

    # 1. 转人工检测
    if is_human_request(user_message):
        try:
            notify_content = f"【网页转人工】\nSession: {session_id}\n消息: {user_message}\n时间: {time.strftime('%Y-%m-%d %H:%M:%S')}"
            success = send_to_wecom(CUSTOMER_SERVICE_USERID, notify_content)
            if success:
                reply = "已为您转接人工客服，我们的客服人员将在企业微信上处理您的请求，请稍等。"
            else:
                reply = "转接人工失败，请稍后再试或联系客服电话。"
        except Exception as e:
            logger.error("转人工异常: %s", e)
            reply = "转接人工时发生内部错误，请稍后再试。"
            success = False
        return jsonify({'reply': reply, 'session_id': session_id, 'human_transferred': success})

    # 2. 无关话题过滤
    if is_out_of_scope(user_message):
        reply = "抱歉，我们只提供钢材产品相关的咨询服务。请问您需要了解哪种钢材（如热轧卷、H型钢、无缝管等）？"
        return jsonify({'reply': reply, 'session_id': session_id})

    # 3. 正常 AI 回复
    history = get_session_history(session_id)
    history.append({"role": "user", "content": user_message})
    history = trim_history(history, max_turns=10)

    # 检索知识库
    knowledge = retrieve_knowledge(user_message)
    messages_for_api = history.copy()
    if knowledge:
        sys_msg = messages_for_api[0]
        sys_msg["content"] = sys_msg["content"] + f"\n\n参考知识库信息：\n{knowledge}"

    headers = {
        'Authorization': f'Bearer {DEEPSEEK_API_KEY}',
        'Content-Type': 'application/json'
    }
    payload = {
        "model": "deepseek-chat",
        "messages": messages_for_api,
        "temperature": 0.7,
        "stream": False
    }

    try:
        resp = requests.post(DEEPSEEK_API_URL, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        ai_reply = resp.json()['choices'][0]['message']['content']

        history.append({"role": "assistant", "content": ai_reply})
        history = trim_history(history, max_turns=10)
        save_session_history(session_id, history)

        return jsonify({'reply': ai_reply, 'session_id': session_id})
    except Exception as e:
        logger.error("DeepSeek API 调用失败: %s", e)
        return jsonify({'error': '服务暂时不可用，请稍后再试'}), 500

@app.route('/wecom/callback', methods=['GET', 'POST'])
def wecom_callback():
    """企业微信回调接口"""
    if request.method == 'GET':
        # URL 验证（文档 3.1）
        signature = request.args.get('msg_signature')
        timestamp = request.args.get('timestamp')
        nonce = request.args.get('nonce')
        echostr = request.args.get('echostr')

        if not all([signature, timestamp, nonce, echostr]):
            return "Missing parameters", 400

        # 解密 echostr 得到明文
        plain = decrypt_wecom_msg(echostr, signature, timestamp, nonce)
        if plain:
            # 返回明文，不带引号、换行
            return plain
        else:
            logger.error("验证失败，返回 403")
            return "Verification failed", 403

    elif request.method == 'POST':
        # 接收消息（文档 3.2）
        raw_data = request.get_data(as_text=True)
        try:
            # 解析 XML 获取 Encrypt 字段
            root = ET.fromstring(raw_data)
            encrypt_msg = root.find('Encrypt').text
            if not encrypt_msg:
                return "success", 200

            # 获取签名参数
            msg_signature = request.args.get('msg_signature')
            timestamp = request.args.get('timestamp')
            nonce = request.args.get('nonce')

            # 解密得到消息 XML
            plain_xml = decrypt_wecom_msg(encrypt_msg, msg_signature, timestamp, nonce)
            if not plain_xml:
                return "success", 200   # 解密失败，不处理

            # 解析 XML 消息结构
            msg_data = parse_wecom_xml(plain_xml)
            if not msg_data or msg_data.get('MsgType') != 'text':
                return "success", 200

            user_id = msg_data.get('FromUserName')
            user_input = msg_data.get('Content', '').strip()

            if not user_input:
                return "success", 200

            # 使用特殊 session_id 区分企业微信用户
            session_id = f"wecom_{user_id}"

            # 检查会话超时（可选）
            if check_activity(session_id):
                # 超时则历史已删除，重新开始
                pass
            update_activity(session_id)

            # 处理转人工
            if is_human_request(user_input):
                notify_content = f"【企微转人工】\n用户: {user_id}\n消息: {user_input}\n时间: {time.strftime('%Y-%m-%d %H:%M:%S')}"
                success = send_to_wecom(CUSTOMER_SERVICE_USERID, notify_content)
                reply_text = "收到，正在为您转接人工客服，请稍候..."
                if not success:
                    reply_text = "转接请求已发送，请稍后。"
                send_to_wecom(user_id, reply_text)   # 回复给用户
                return "success", 200

            # 无关话题过滤
            if is_out_of_scope(user_input):
                reply_text = "抱歉，我们只提供钢材产品相关的咨询服务。"
                send_to_wecom(user_id, reply_text)
                return "success", 200

            # AI 对话
            history = get_session_history(session_id)
            history.append({"role": "user", "content": user_input})
            history = trim_history(history, max_turns=10)

            knowledge = retrieve_knowledge(user_input)
            messages_for_api = history.copy()
            if knowledge:
                sys_msg = messages_for_api[0]
                sys_msg["content"] = sys_msg["content"] + f"\n\n参考知识库信息：\n{knowledge}"

            headers = {
                'Authorization': f'Bearer {DEEPSEEK_API_KEY}',
                'Content-Type': 'application/json'
            }
            payload = {
                "model": "deepseek-chat",
                "messages": messages_for_api,
                "temperature": 0.7,
                "stream": False
            }

            try:
                resp = requests.post(DEEPSEEK_API_URL, json=payload, headers=headers, timeout=30)
                resp.raise_for_status()
                ai_reply = resp.json()['choices'][0]['message']['content']

                history.append({"role": "assistant", "content": ai_reply})
                history = trim_history(history, max_turns=10)
                save_session_history(session_id, history)

                send_to_wecom(user_id, ai_reply)
            except Exception as e:
                logger.error("DeepSeek API 错误: %s", e)
                send_to_wecom(user_id, "抱歉，AI 服务暂时繁忙，请稍后再试。")

            return "success", 200

        except Exception as e:
            logger.error("处理企微回调失败: %s", e, exc_info=True)
            return "success", 200

@app.route('/health', methods=['GET'])
def health():
    """健康检查"""
    return jsonify({"status": "ok"})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    # 生产环境 debug=False
    app.run(host='0.0.0.0', port=port, debug=False)