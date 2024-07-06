import logging
import os
import re
import sys
from datetime import datetime
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
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

import google.generativeai as genai
from firebase import firebase

firebase_url = os.getenv('FIREBASE_URL')
gemini_key = os.getenv('GEMINI_API_KEY')
genai.configure(api_key=gemini_key)

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
        text = event.message.text
        user_id = event.source.user_id

        msg_type = event.message.type
        fdb = firebase.FirebaseApplication(firebase_url, None)
        if event.source.type == 'group':
            user_chat_path = f'chat/{event.source.group_id}'
        else:
            user_chat_path = f'chat/{user_id}'
            chatgpt = fdb.get(user_chat_path, None)

        if msg_type == 'text':
            if chatgpt is None:
                messages = []
            else:
                messages = chatgpt

            bot_condition = {
                "出題": 'Q',
                "解析": 'A'
            }

            model = genai.GenerativeModel('gemini-1.5-pro')
            response = model.generate_content(
                f'請判斷 {text} 裡面的文字屬於 {bot_condition} 裡面的哪一項？符合條件請回傳對應的英文文字就好，不要有其他的文字與字元。')
            text_condition = re.sub(r'[^A-Za-z]', '', response.text)

            if text_condition == 'Q':
                response = model.generate_content(
                    f'假設你是一個詐騙者，寫一段騙人的訊息。')
                messages.append({'role': 'bot', 'parts': [response.text]})
                reply_msg = response.text
                fdb.put_async(user_chat_path, None, messages)
            elif text_condition == 'A':
                if len(messages) > 0 and messages[-1]['role'] == 'bot':
                    scam_message = messages[-1]['parts'][0]
                    advice = analyze_response(scam_message)
                    reply_msg = f'你的回覆是: {text}\n\n辨別建議:\n{advice}'
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
    # 這裡是您的分析邏輯，根據詐騙訊息給出辨別建議
    advice = "這是一條詐騙訊息，你可以注意到其中的誇張語氣和不合理的要求。"
    return advice

if __name__ == "__main__":
    port = int(os.environ.get('PORT', default=8080))
    debug = True if os.environ.get(
        'API_ENV', default='develop') == 'develop' else False
    logging.info('Application will start...')
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=debug)
