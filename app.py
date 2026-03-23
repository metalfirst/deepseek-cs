import os
import json
import uuid
import time
import hashlib
import base64
import struct
import logging
import re
import xml.etree.ElementTree as ET
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from Crypto.Cipher import AES
import requests

app = Flask(__name__)
CORS(app)

# ==================== 日志配置 ====================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ==================== 配置（从环境变量读取） ====================
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"

WECOM_CORP_ID = "ww2e3b75e9697e5c62"
WECOM_AGENT_ID = 1000005
WECOM_SECRET = os.environ.get("WECOM_SECRET")
CUSTOMER_SERVICE_USERID = os.environ.get("CUSTOMER_SERVICE_USERID", "YangJun")

# 回调配置（用于接收客服回复）
WECOM_TOKEN = os.environ.get("WECOM_TOKEN")
WECOM_ENCODING_AES_KEY = os.environ.get("WECOM_ENCODING_AES_KEY")

ACTIVITY_TIMEOUT = 5 * 60   # 5分钟

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

# ==================== 会话管理（内存） ====================
memory_store = {}          # {session_id: {"history": list, "last_active": float, "mode": str, "pending_reply": str}}
last_human_session = {}    # {客服userid: session_id} 用于简化客服回复

def get_session_data(session_id):
    if session_id not in memory_store:
        history = [{"role": "system", "content": SYSTEM_PROMPT}]
        memory_store[session_id] = {
            "history": history,
            "last_active": time.time(),
            "mode": "auto",
            "pending_reply": ""
        }
        return memory_store[session_id]
    return memory_store[session_id]

def get_session_history(session_id):
    return get_session_data(session_id)["history"]

def save_session_history(session_id, history):
    memory_store[session_id]["history"] = history
    memory_store[session_id]["last_active"] = time.time()

def check_activity(session_id):
    if session_id in memory_store:
        last = memory_store[session_id]["last_active"]
        if time.time() - last > ACTIVITY_TIMEOUT:
            del memory_store[session_id]
            return True
    return False

def update_activity(session_id):
    if session_id in memory_store:
        memory_store[session_id]["last_active"] = time.time()

def trim_history(history, max_turns=10):
    if len(history) <= max_turns * 2 + 1:
        return history
    return [history[0]] + history[-(max_turns * 2):]

# ==================== 知识库检索 ====================
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

# ==================== 转人工检测 ====================
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

# ==================== 企业微信主动发送 ====================
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
    if not WECOM_TOKEN or not WECOM_ENCODING_AES_KEY:
        return None
    arr = [WECOM_TOKEN, timestamp, nonce, encrypt_msg]
    arr.sort()
    tmp_str = "".join(arr)
    computed = hashlib.sha1(tmp_str.encode()).hexdigest()
    if computed != msg_signature:
        logger.warning("签名验证失败")
        return None
    try:
        aes_key = base64.b64decode(WECOM_ENCODING_AES_KEY + "=")
        cipher = AES.new(aes_key, AES.MODE_CBC, aes_key[:16])
        decrypted = cipher.decrypt(base64.b64decode(encrypt_msg))
        pad = decrypted[-1]
        content = decrypted[16:-pad]
        xml_len = struct.unpack("!I", content[:4])[0]
        plain_text = content[4:4+xml_len].decode('utf-8')
        return plain_text
    except Exception as e:
        logger.error("解密失败: %s", e)
        return None

def parse_wecom_xml(xml_str):
    try:
        root = ET.fromstring(xml_str)
        return {child.tag: child.text for child in root}
    except Exception as e:
        logger.error("解析 XML 失败: %s", e)
        return None

# ==================== 网页路由 ====================
@app.route('/')
def index():
    return render_template('chat.html')

@app.route('/api/chat', methods=['POST'])
def chat():
    data = request.json
    user_message = data.get('message', '').strip()
    session_id = data.get('session_id', '')

    if not user_message:
        return jsonify({'error': '消息不能为空'}), 400

    # 会话管理
    if not session_id:
        session_id = str(uuid.uuid4())
        logger.info("新会话创建: %s", session_id)
    else:
        if check_activity(session_id):
            new_session_id = str(uuid.uuid4())
            reply = "由于长时间未活动，会话已结束。您可以重新开始对话。"
            get_session_data(new_session_id)
            update_activity(new_session_id)
            logger.info("会话 %s 已超时，新会话: %s", session_id, new_session_id)
            return jsonify({'reply': reply, 'session_id': new_session_id})

    session_data = get_session_data(session_id)
    update_activity(session_id)

    # 转人工检测
    if is_human_request(user_message):
        try:
            session_data["mode"] = "human"
            last_human_session[CUSTOMER_SERVICE_USERID] = session_id
            notify_content = f"【网页转人工】\n会话ID: {session_id}\n消息: {user_message}\n您可以直接回复内容，系统将自动发送给该用户。"
            success = send_to_wecom(CUSTOMER_SERVICE_USERID, notify_content)
            reply = "已为您转接人工客服，客服将尽快回复您。" if success else "转接人工失败，请稍后再试。"
            logger.info("转人工: 会话 %s, 发送通知结果 %s", session_id, success)
        except Exception as e:
            logger.error("转人工异常: %s", e)
            reply = "转接人工时发生内部错误。"
            success = False
        return jsonify({'reply': reply, 'session_id': session_id, 'human_transferred': success})

    # 人工模式：转发消息给客服
    if session_data["mode"] == "human":
        forward_content = f"【用户消息】\n会话: {session_id}\n{user_message}"
        send_to_wecom(CUSTOMER_SERVICE_USERID, forward_content)
        logger.info("人工模式：转发用户消息，会话 %s", session_id)
        return jsonify({'reply': '您的消息已转交人工客服，请耐心等待回复。', 'session_id': session_id})

    # 无关话题过滤（仅自动模式）
    if is_out_of_scope(user_message):
        reply = "抱歉，我们只提供钢材产品相关的咨询服务。请问您需要了解哪种钢材？"
        return jsonify({'reply': reply, 'session_id': session_id})

    # 自动模式：AI 回复
    history = session_data["history"]
    history.append({"role": "user", "content": user_message})
    history = trim_history(history)

    knowledge = retrieve_knowledge(user_message)
    messages = history.copy()
    if knowledge:
        messages[0]["content"] += f"\n\n参考知识库信息：\n{knowledge}"

    headers = {'Authorization': f'Bearer {DEEPSEEK_API_KEY}', 'Content-Type': 'application/json'}
    payload = {"model": "deepseek-chat", "messages": messages, "temperature": 0.7, "stream": False}
    try:
        resp = requests.post(DEEPSEEK_API_URL, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        ai_reply = resp.json()['choices'][0]['message']['content']
        history.append({"role": "assistant", "content": ai_reply})
        history = trim_history(history)
        session_data["history"] = history
        logger.info("AI回复: 会话 %s, 长度 %d", session_id, len(ai_reply))
        return jsonify({'reply': ai_reply, 'session_id': session_id})
    except Exception as e:
        logger.error("DeepSeek API 调用失败: %s", e)
        return jsonify({'error': '服务暂时不可用，请稍后再试'}), 500

# ==================== 轮询接口 ====================
@app.route('/api/poll', methods=['GET'])
def poll():
    session_id = request.args.get('session_id')
    if not session_id:
        return jsonify({'error': '缺少 session_id'}), 400
    session_data = memory_store.get(session_id)
    if not session_data:
        return jsonify({'reply': None})
    pending = session_data.get("pending_reply", "")
    if pending:
        session_data["pending_reply"] = ""
        logger.info("轮询返回客服回复，会话 %s", session_id)
        return jsonify({'reply': pending})
    else:
        return jsonify({'reply': None})

# ==================== 企业微信回调 ====================
@app.route('/wecom/callback', methods=['GET', 'POST'])
def wecom_callback():
    if request.method == 'GET':
        signature = request.args.get('msg_signature')
        timestamp = request.args.get('timestamp')
        nonce = request.args.get('nonce')
        echostr = request.args.get('echostr')
        if not all([signature, timestamp, nonce, echostr]):
            return "Missing parameters", 400
        plain = decrypt_wecom_msg(echostr, signature, timestamp, nonce)
        if plain:
            return plain
        else:
            return "Verification failed", 403

    elif request.method == 'POST':
        raw_data = request.get_data(as_text=True)
        try:
            root = ET.fromstring(raw_data)
            encrypt_msg = root.find('Encrypt').text
            if not encrypt_msg:
                return "success", 200
            msg_signature = request.args.get('msg_signature')
            timestamp = request.args.get('timestamp')
            nonce = request.args.get('nonce')
            plain_xml = decrypt_wecom_msg(encrypt_msg, msg_signature, timestamp, nonce)
            if not plain_xml:
                return "success", 200
            msg_data = parse_wecom_xml(plain_xml)
            if not msg_data or msg_data.get('MsgType') != 'text':
                return "success", 200

            from_user = msg_data.get('FromUserName')
            content = msg_data.get('Content', '').strip()
            logger.info("收到企业微信消息: from=%s, content=%s", from_user, content)

            # 如果是客服的回复
            if from_user == CUSTOMER_SERVICE_USERID:
                # 尝试解析“回复 <session_id> 内容”格式
                match = re.match(r'回复\s+([\w-]+)\s+(.*)', content)
                if match:
                    session_id = match.group(1)
                    reply_text = match.group(2)
                    logger.info("指定会话模式，会话ID: %s", session_id)
                else:
                    # 未指定会话ID，使用最近一次转人工的会话
                    session_id = last_human_session.get(from_user)
                    if not session_id:
                        logger.warning("未找到待回复的会话，客服需使用格式：回复 <会话ID> 内容")
                        send_to_wecom(from_user, "没有找到待回复的会话，请使用格式：回复 <会话ID> 内容")
                        return "success", 200
                    reply_text = content
                    logger.info("自动模式，使用最近会话: %s", session_id)

                # 保存回复
                if session_id in memory_store:
                    memory_store[session_id]["pending_reply"] = reply_text
                    send_to_wecom(from_user, f"已发送回复给用户 {session_id}")
                    logger.info("已保存回复到会话 %s", session_id)
                else:
                    logger.warning("未找到会话 %s", session_id)
                    send_to_wecom(from_user, f"未找到会话 {session_id}")
                return "success", 200

            # 其他消息（普通用户发来的消息）可忽略
            logger.info("忽略非客服消息")
            return "success", 200

        except Exception as e:
            logger.error("处理企微回调失败: %s", e, exc_info=True)
            return "success", 200

# ==================== 企业微信域名验证 ====================
@app.route('/WW_verify_5nyB6B5oVM0zoiCr.txt')
def wecom_verify():
    return "5nyB6B5oVM0zoiCr"

# ==================== 健康检查 ====================
@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok"})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)