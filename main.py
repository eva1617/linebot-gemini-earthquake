from fastapi import FastAPI, HTTPException, Request
import logging
import os
import re
import sys
from datetime import datetime
from dotenv import load_dotenv
from linebot.v3.webhook import WebhookParser
from linebot.v3.messaging import (
    AsyncApiClient,
    AsyncMessagingApi,
    Configuration,
    ReplyMessageRequest,
    TextMessage
)
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import MessageEvent, TextMessageContent

import uvicorn
import requests
import google.generativeai as genai
from firebase import firebase
import random

logging.basicConfig(level=os.getenv('LOG', 'WARNING'))
logger = logging.getLogger(__file__)

app = FastAPI()

load_dotenv()
channel_secret = os.getenv('LINE_CHANNEL_SECRET', None)
channel_access_token = os.getenv('LINE_CHANNEL_ACCESS_TOKEN', None)
if channel_secret is None:
    print('Specify LINE_CHANNEL_SECRET as environment variable.')
    sys.exit(1)
if channel_access_token is None:
    print('Specify LINE_CHANNEL_ACCESS_TOKEN as environment variable.')
    sys.exit(1)

configuration = Configuration(access_token=channel_access_token)
async_api_client = AsyncApiClient(configuration)
line_bot_api = AsyncMessagingApi(async_api_client)
parser = WebhookParser(channel_secret)

firebase_url = os.getenv('FIREBASE_URL')
gemini_key = os.getenv('GEMINI_API_KEY')
genai.configure(api_key=gemini_key)

scam_templates = [
    "【國泰世華】您的銀行賬戶顯示異常，請立即登入綁定用戶資料，否則賬戶將凍結使用 {url}",
    "我朋友參加攝影比賽麻煩幫忙投票 {url}",
    "登入FB就投票成功了我手機當機 line用不了 想請你幫忙安全認證 幫我收個認證簡訊 謝謝 你LINE的登陸認證密碼記得嗎 認證要用到 確認是本人幫忙認證",
    "您的LINE已違規使用，將在24小時內註銷，請使用谷歌瀏覽器登入電腦網站並掃碼驗證解除違規 {url}",
    "【台灣自來水公司】貴戶本期水費已逾期，總計新台幣395元整，務請於6月16日前處理繳費，詳情繳費：{url} 若再超過上述日期，將終止供水",
    "萬聖節快樂🎃 活動免費貼圖無限量下載 {url}",
    "【台灣電力股份有限公司】貴戶本期電費已逾期，總計新台幣1058元整，務請於6月14日前處理繳費，詳情繳費：{url}，若再超過上述日期，將停止收費"
]

fake_url_source = "https://www-api.moda.gov.tw/OpenData/Files/12998"

def get_fake_urls():
    try:
        response = requests.get(fake_url_source)
        response.raise_for_status()
        urls = response.text.split('\n')
        return [url.strip() for url in urls if url.strip()]
    except requests.RequestException as e:
        logger.error(f"Error fetching fake URLs: {e}")
        return []

fake_urls = get_fake_urls()

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
        logging.info(event)
        if not isinstance(event, MessageEvent):
            continue
        if not isinstance(event.message, TextMessageContent):
            continue
        text = event.message.text.strip()
        user_id = event.source.user_id

        fdb = firebase.FirebaseApplication(firebase_url, None)
        if event.source.type == 'group':
            user_chat_path = f'chat/{event.source.group_id}'
        else:
            user_chat_path = f'chat/{user_id}'
        chatgpt = fdb.get(user_chat_path, None)

        if text == "出題":
            scam_template = random.choice(scam_templates)
            fake_url = random.choice(fake_urls) if fake_urls else "http://example.com"
            scam_example = scam_template.format(url=fake_url)
            messages = [{'role': 'bot', 'parts': [scam_example]}]
            fdb.put_async(user_chat_path, None, messages)
            reply_msg = scam_example
        elif text == "解析":
            if chatgpt and len(chatgpt) > 0 and chatgpt[-1]['role'] == 'bot':
                scam_message = chatgpt[-1]['parts'][0]
                advice = analyze_response(scam_message)
                reply_msg = f'上次的詐騙訊息是: {scam_message}\n\n辨別建議:\n{advice}'
            else:
                reply_msg = '目前沒有可供解析的訊息，請先出題。'
        else:
            reply_msg = '未能識別的指令，請輸入 "出題" 或 "解析"。'

        await line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply_msg)]
            ))

    return 'OK'

def analyze_response(text):
    advice = []
    # Check for suspicious URLs
    if re.search(r'\bwww\.[a-zA-Z0-9-]+\.[a-z]{2,}\b', text):
        advice.append("這條訊息包含可疑的網址，請勿點擊。")
    
    # Check for urgency or threat language
    if re.search(r'\b(逾期|凍結|註銷|終止供水|停止收費|登入|認證|綁定用戶資料|立即|緊急)\b', text):
        advice.append("訊息中包含緊急措辭，這是常見的詐騙手段。")
    
    # Check for inducement phrases
    if re.search(r'\b(點擊此處|請立即|詳情繳費|免費|下載|活動|投票)\b', text):
        advice.append("訊息中包含誘導性語句，這可能是詐騙。")
    
    # Check for unsolicited requests
    if re.search(r'\b(幫忙|要求|收個認證|麻煩幫忙|確認是本人幫忙認證|幫忙認證)\b', text):
        advice.append("訊息中包含不明請求，這可能是詐騙手段之一。")
    
    # Check for uncommon domain extensions
    if re.search(r'\.(icu|info|bit|pgp|shop)\b', text):
        advice.append("訊息中包含不常見的域名擴展，請小心。")

    # Check for signs of phishing (e.g., login, account details)
    if re.search(r'\b(登入|用戶資料|帳戶|賬戶|安全認證)\b', text):
        advice.append("訊息中要求提供帳戶或個人資料，這可能是網絡釣魚詐騙。")
    
    if not advice:
        advice.append("這條訊息看起來很可疑，請小心處理。")

    return "\n".join(advice)

if __name__ == "__main__":
    port = int(os.environ.get('PORT', default=8080))
    debug = True if os.environ.get(
        'API_ENV', default='develop') == 'develop' else False
    logging.info('Application will start...')
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=debug)
