import os
import json
import uuid
import time
import requests
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ==================== 配置（建议使用环境变量） ====================
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "YOUR_API_KEY")
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"

# 企业微信配置
WECOM_CORP_ID = "ww2e3b75e9697e5c62"
WECOM_AGENT_ID = 1000005
WECOM_SECRET = os.environ.get("WECOM_SECRET", "YOUR_SECRET")
CUSTOMER_SERVICE_USERID = "YangJun"   # 请替换为实际客服成员账号

# 会话超时（秒）
ACTIVITY_TIMEOUT = 5 * 60   # 5分钟

# 知识库文件（与 app.py 同目录）
KNOWLEDGE_FILE = "knowledge.txt"
knowledge_text = ""

if os.path.exists(KNOWLEDGE_FILE):
    try:
        with open(KNOWLEDGE_FILE, "r", encoding="utf-8") as f:
            knowledge_text = f.read()
    except Exception as e:
        print(f"读取知识库失败: {e}")

# 系统提示词
SYSTEM_PROMPT = (
    "你是上海巨红贸易有限公司（Unionmetal Trading）的AI客服助手。\n"
    "公司主营钢材出口：钢卷（热轧、冷轧、不锈钢）、钢管（无缝、焊接、方矩管）、型钢（角钢、槽钢、工字钢、H型钢）。\n"
    "你的任务是：\n"
    "1. 用客户使用的语言（中文、英语、阿拉伯语等）回复。\n"
    "2. 回答关于产品规格、标准、最小起订量、交货期、付款方式等专业问题。\n"
    "3. 如果客户询问价格，引导其提供具体需求（产品、规格、数量、目的港）并告知将转销售跟进。\n"
    "4. 当客户表示需要人工帮助时（如'转人工'、'human'、'بشري'），记录需求并告知将通知人工客服。\n"
    "5. 保持专业、友好、简洁的语调。"
)

# ==================== 内存存储 ====================
memory_store = {}       # 会话历史: {session_id: history_json}
activity_store = {}     # 最后活动时间: {session_id: timestamp}

def get_session_history(session_id):
    if session_id not in memory_store:
        history = [{"role": "system", "content": SYSTEM_PROMPT}]
        memory_store[session_id] = json.dumps(history)
        return history
    return json.loads(memory_store[session_id])

def save_session_history(session_id, history):
    memory_store[session_id] = json.dumps(history)

def check_activity(session_id):
    last = activity_store.get(session_id, 0)
    if last == 0 or time.time() - last > ACTIVITY_TIMEOUT:
        if session_id in memory_store:
            del memory_store[session_id]
        if session_id in activity_store:
            del activity_store[session_id]
        return True
    return False

def update_activity(session_id):
    activity_store[session_id] = time.time()

def trim_history(history, max_turns=10):
    """保留系统消息 + 最近 max_turns 轮对话"""
    if len(history) <= max_turns * 2 + 1:
        return history
    return [history[0]] + history[-(max_turns * 2):]

# ==================== 企业微信 API ====================
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
            print("获取 token 失败:", resp)
    except Exception as e:
        print("请求 token 异常:", e)
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
            print("发送消息失败:", resp)
            return False
    except Exception as e:
        print("发送消息异常:", e)
        return False

# ==================== 多语言转人工检测 ====================
def is_human_request(message):
    """检测用户是否请求转人工（支持中文、英文、阿拉伯语）"""
    # 中文
    cn_keywords = ["转人工", "人工客服", "人工服务", "找人工", "人工", "真人", "找客服"]
    # 英文
    en_keywords = [
        "human", "agent", "speak to human", "talk to human",
        "customer service", "support", "real person", "live agent",
        "transfer to human", "human agent"
    ]
    # 阿拉伯语
    ar_keywords = [
        "بشري", "دعم", "خدمة العملاء", "وكيل", "التحدث إلى بشري",
        "تحويل إلى وكيل", "مساعد بشري"
    ]
    all_keywords = cn_keywords + en_keywords + ar_keywords
    msg_lower = message.lower()
    return any(k.lower() in msg_lower for k in all_keywords)

# ==================== 知识库检索（简单关键词匹配） ====================
def retrieve_knowledge(query):
    """从知识库中检索与查询相关的文本（基于关键词）"""
    if not knowledge_text:
        return ""
    # 将查询分词，取所有非空词
    keywords = [w for w in query.lower().split() if len(w) > 1]
    if not keywords:
        return ""
    lines = knowledge_text.split('\n')
    matched = []
    for line in lines:
        line_lower = line.lower()
        if any(k in line_lower for k in keywords):
            matched.append(line)
    # 最多返回3条，避免上下文过长
    return "\n".join(matched[:3])

# ==================== Flask 路由 ====================
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
    else:
        if check_activity(session_id):
            reply = "由于长时间未活动，会话已结束。您可以重新开始对话。"
            update_activity(session_id)
            return jsonify({'reply': reply, 'session_id': session_id})

    update_activity(session_id)

    # 转人工检测
    if is_human_request(user_message):
        try:
            notify_content = f"【Human Request】\nSession: {session_id}\nMessage: {user_message}\nPlease handle."
            success = send_to_wecom(CUSTOMER_SERVICE_USERID, notify_content)
            if success:
                reply = "已为您转接人工客服，我们的客服人员将在企业微信上处理您的请求，请稍等。"
            else:
                reply = "转接人工失败，请稍后再试或联系客服电话。"
        except Exception as e:
            print("转人工异常:", e)
            reply = "转接人工时发生内部错误，请稍后再试。"
            success = False
        return jsonify({'reply': reply, 'session_id': session_id, 'human_transferred': success})

    # 正常 AI 回复
    history = get_session_history(session_id)
    history.append({"role": "user", "content": user_message})
    history = trim_history(history, max_turns=10)

    # 检索知识库，如果找到相关内容，将其作为系统消息的补充
    knowledge = retrieve_knowledge(user_message)
    messages_for_api = history.copy()
    if knowledge:
        # 在系统消息之后插入一条临时知识消息（作为系统角色）
        # 但注意 history 中第一条是系统消息，我们将知识拼接到原系统消息中更简单
        sys_msg = messages_for_api[0]
        sys_msg["content"] = sys_msg["content"] + f"\n\n参考知识库信息：\n{knowledge}"
        # 或者添加一条临时用户消息携带知识，两者都可，这里选择直接扩写系统消息

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
    except requests.exceptions.RequestException as e:
        print(f"DeepSeek API 调用失败: {e}")
        return jsonify({'error': '服务暂时不可用，请稍后再试'}), 500
    except Exception as e:
        print(f"未知错误: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': '服务器内部错误'}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)), debug=False)