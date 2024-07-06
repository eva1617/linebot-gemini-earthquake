from fastapi import FastAPI, HTTPException, Request
import logging
import os
import re
import sys
from dotenv import load_dotenv
from linebot import WebhookParser, LineBotApi
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import uvicorn
import random
from firebase import firebase
import google.generativeai as genai

logging.basicConfig(level=os.getenv('LOG', 'WARNING'))
logger = logging.getLogger(__file__)

app = FastAPI()

load_dotenv()
channel_secret = os.getenv('LINE_CHANNEL_SECRET')
channel_access_token = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
firebase_url = os.getenv('FIREBASE_URL')
gemini_key = os.getenv('GEMINI_API_KEY')

line_bot_api = LineBotApi(channel_access_token)
parser = WebhookParser(channel_secret)
firebase_app = firebase.FirebaseApplication(firebase_url, None)

scam_templates = [
    "【國泰世華】您的銀行賬戶顯示異常，請立即登入綁定用戶資料，否則賬戶將凍結使用 www.cathay-bk.com",
    "我朋友參加攝影比賽麻煩幫忙投票 http://www.yahoonikk.info/page/vote.pgp?pid=51",
    "登入FB就投票成功了我手機當機 line用不了 想請你幫忙安全認證 幫我收個認證簡訊 謝謝 你LINE的登陸認證密碼記得嗎 認證要用到 確認是本人幫忙認證",
    "您的LINE已違規使用，將在24小時內註銷，請使用谷歌瀏覽器登入電腦網站並掃碼驗證解除違規 www.line-wbe.icu",
    "【台灣自來水公司】貴戶本期水費已逾期，總計新台幣395元整，務請於6月16日前處理繳費，詳情繳費：https://bit.ly/4cnMNtE 若再超過上述日期，將終止供水",
    "萬聖節快樂🎃 活動免費貼圖無限量下載 https://lineeshop.com",
    "【台灣電力股份有限公司】貴戶本期電費已逾期，總計新台幣1058元整，務請於6月14日前處理繳費，詳情繳費：(網址)，若再超過上述日期，將停止收費"
]

@app.get("/health")
async def health():
    return 'ok'

@app.post("/webhooks/line")
async def handle_callback(request: Request):
    signature = request.headers['X-Line-Signature']

    body = await request.body()
    body = body.decode()

    try:
        events = parser.parse(body, signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    for event in events:
        if not isinstance(event, MessageEvent) or not isinstance(event.message, TextMessage):
            continue
        
        text = event.message.text.strip()
        user_id = event.source.user_id

        if event.source.type == 'group':
            user_chat_path = f'chat/{event.source.group_id}'
        else:
            user_chat_path = f'chat/{user_id}'

        chatgpt = firebase_app.get(user_chat_path, None)

        if text == "出題":
            scam_example = generate_scam_example()
            messages = [{'role': 'bot', 'parts': [scam_example]}]
            firebase_app.put_async(user_chat_path, None, messages)
            reply_msg = scam_example
        elif text == "解析":
            if chatgpt and len(chatgpt) > 0 and chatgpt[-1]['role'] == 'bot':
                scam_message = chatgpt[-1]['parts'][0]
                advice = analyze_response(scam_message)
                reply_msg = f'詐騙訊息分析:\n\n{advice}'
                # Add points to the user
                add_points(user_id, 5)
            else:
                reply_msg = '目前沒有可供解析的訊息，請先輸入「出題」生成一個範例。'
        else:
            reply_msg = '未能識別的指令，請輸入「出題」生成一個詐騙訊息範例，或輸入「解析」來分析上一個生成的範例。'

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply_msg)
        )

    return 'OK'

def generate_scam_example():
    template = random.choice(scam_templates)
    prompt = (
        f"以下是一個詐騙訊息範例:\n\n{template}\n\n"
        "請根據這個範例生成一個新的、類似的詐騙訊息。保持相似的結構和風格，"
        "但改變具體內容。請確保新生成的訊息具有教育性質，可以用於提高人們對詐騙的警惕性。"
        "只需要生成詐騙訊息本身，不要添加任何額外的說明或指示。"
    )
    
    model = genai.GenerativeModel('gemini-pro')
    response = model.generate_content(prompt)
    return response.text.strip()

def analyze_response(text):
    prompt = (
        f"以下是一條潛在的詐騙訊息:\n\n{text}\n\n"
        "請分析這條訊息，並提供詳細的辨別建議。包括以下幾點：\n"
        "1. 這條訊息中的可疑元素\n"
        "2. 為什麼這些元素是可疑的\n"
        "3. 如何識別類似的詐騙訊息\n"
        "4. 面對這種訊息時應該採取什麼行動\n"
        "請以教育性和提醒性的語氣回答，幫助人們提高警惕。不要使用粗體或任何特殊格式，只需使用純文本。"
    )
    
    model = genai.GenerativeModel('gemini-pro')
    response = model.generate_content(prompt)
    return response.text.strip()

def add_points(user_id, points):
    # Implement your logic to add points to the user identified by user_id
    # This could involve updating a database or some other persistent storage
    pass

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8080))
    debug = os.environ.get('API_ENV', 'develop') == 'develop'
    logging.info('Starting the application...')
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=debug)
