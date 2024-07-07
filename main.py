from fastapi import FastAPI, HTTPException, Request
import logging
import os
import sys
from dotenv import load_dotenv
from linebot import (
    LineBotApi, WebhookParser
)
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, ConfirmTemplate, MessageAction, TemplateSendMessage
)
from firebase import firebase
import random
import uvicorn
import google.generativeai as genai

logging.basicConfig(level=os.getenv('LOG', 'WARNING'))
logger = logging.getLogger(__file__)

app = FastAPI()

load_dotenv()
channel_secret = os.getenv('LINE_CHANNEL_SECRET', None)
channel_access_token = os.getenv('LINE_CHANNEL_ACCESS_TOKEN', None)
if channel_secret is None or channel_access_token is None:
    logger.error('Specify LINE_CHANNEL_SECRET and LINE_CHANNEL_ACCESS_TOKEN as environment variables.')
    sys.exit(1)

line_bot_api = LineBotApi(channel_access_token)
parser = WebhookParser(channel_secret)

firebase_url = os.getenv('FIREBASE_URL')
gemini_key = os.getenv('GEMINI_API_KEY')
genai.configure(api_key=gemini_key)

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

        user_id = event.source.user_id
        fdb = firebase.FirebaseApplication(firebase_url, None)
        user_score_path = f'scores/{user_id}'
        user_score = fdb.get(user_score_path, None) or 0

        if event.message.text == '出題':
            scam_example, correct_example = generate_examples()
            messages = [{'role': 'bot', 'parts': [scam_example, correct_example]}]
            fdb.put_async(f'chat/{user_id}', None, messages)
            reply_msg = f"{scam_example}\n\n請判斷這是否為詐騙訊息"
            confirm_template = ConfirmTemplate(
                text='請判斷是否為詐騙訊息。',
                actions=[
                    MessageAction(label='是', text='是'),
                    MessageAction(label='否', text='否')
                ]
            )
            template_message = TemplateSendMessage(alt_text='出題', template=confirm_template)
            line_bot_api.reply_message(event.reply_token, [TextSendMessage(text=reply_msg), template_message])
        elif event.message.text == '分數':
            reply_msg = f"你的當前分數是：{user_score}分"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_msg))
        elif event.message.text in ['是', '否']:
            chatgpt = fdb.get(f'chat/{user_id}', None)
            if chatgpt and len(chatgpt) > 0 and chatgpt[-1]['role'] == 'bot':
                scam_message, correct_message = chatgpt[-1]['parts']
                is_scam = scam_message is not None
                user_response = event.message.text == '是'

                if user_response == is_scam:
                    user_score += 50
                    fdb.put_async(user_score_path, None, user_score)
                    reply_msg = f"你好棒！你的當前分數是：{user_score}分"
                else:
                    user_score -= 50
                    if user_score < 50:
                        user_score = 0
                    fdb.put_async(user_score_path, None, user_score)
                    if is_scam:
                        reply_msg = f"這是詐騙訊息。請點選解析了解更多。"
                    else:
                        advice = analyze_response(correct_message, is_scam, user_response)
                        reply_msg = f"這是正確訊息。分析如下:\n\n{advice}\n\n你的當前分數是：{user_score}分"
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_msg))
            else:
                reply_msg = '目前沒有可供解析的訊息，請先輸入「出題」生成一個範例。'
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_msg))
        elif event.message.text == "解析":
            chatgpt = fdb.get(f'chat/{user_id}', None)
            if chatgpt and len(chatgpt) > 0 and chatgpt[-1]['role'] == 'bot':
                scam_message, correct_message = chatgpt[-1]['parts']
                is_scam = scam_message is not None
                advice = analyze_response(scam_message if is_scam else correct_message, is_scam, True)
                reply_msg = f"分析如下:\n\n{advice}"
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_msg))
            else:
                reply_msg = '請先回答「是」或「否」來判斷詐騙訊息，再查看解析。'
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_msg))
        elif event.message.text == "排行榜":
            reply_msg = get_rank(user_id, firebase_url)
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_msg))
        else:
            reply_msg = '請先回答「是」或「否」來判斷詐騙訊息，再查看解析。'
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_msg))

    return 'OK'

def generate_examples():
    scam_template = random.choice(scam_templates)
    prompt_scam = (
        f"以下是一個詐騙訊息範例:\n\n{scam_template}\n\n"
        "請根據這個範例生成一個新的、類似的詐騙訊息。保持相似的結構和風格，"
        "但改變具體內容。請確保新生成的訊息具有教育性質，可以用於提高人們對詐騙的警惕性。"
        "只需要生成詐騙訊息本身，不要添加任何額外的說明或指示。"
    )
    prompt_correct = (
        f"請生成一個真實且正確的訊息範例，其風格和結構類似於以下的詐騙訊息範例，但內容是真實且正確的:\n\n{scam_template}"
    )

    model = genai.GenerativeModel('gemini-pro')
    scam_response = model.generate_content(prompt_scam)
    correct_response = model.generate_content(prompt_correct)
    return scam_response.text.strip(), correct_response.text.strip()

def analyze_response(text, is_scam, user_response):
    if user_response == is_scam:
        if is_scam:
            prompt = (
                f"以下是一個詐騙訊息:\n\n{text}\n\n"
                "請解釋這條訊息是如何詐騙的，並提供相應的應對策略。"
            )
        else:
            prompt = (
                f"以下是一條真實且正確的訊息:\n\n{text}\n\n"
                "請分析這條訊息，並提供詳細的解釋，說明這條訊息是真實且正確的，"
                "包括內容的合理性、可信度來源等。"
            )

        model = genai.GenerativeModel('gemini-pro')
        response = model.generate_content(prompt)
        return response.text.strip()
    else:
        return "無法分析，請提供正確的回答"

def get_sorted_scores(firebase_url, path):
    fdb = firebase.FirebaseApplication(firebase_url, None)
    scores = fdb.get(path, None)
    
    if scores:
        score_list = [(user, score) for user, score in scores.items()]
        sorted_score_list = sorted(score_list, key=lambda x: x[1], reverse=True)
        return sorted_score_list
    else:
        return []

def get_rank(current_user_id, firebase_url):
    rank_width = 7
    user_width = 14
    score_width = 11
    total_width = rank_width + user_width + score_width + 4

    sorted_scores = get_sorted_scores(firebase_url, 'scores/')

    table_str = ''

    table_str += '+' + '-' * total_width + '+\n'
    table_str += '|' + "排行榜".center(total_width - 3) + '|\n'
    table_str += '+' + '-' * total_width + '+\n'
    table_str += f"|{'排名'.center(rank_width)}|{'User'.center(user_width)}|{'Score'.center(score_width)}|\n"
    table_str += '+' + '-' * rank_width + '+' + '-' * user_width + '+' + '-' * score_width + '+\n'

    if sorted_scores:
        i = 1
        for user, score in sorted_scores:
            if user == current_user_id:
                user_display = f'*{user[:user_width]}*'
            else:
                user_display = user[:5]

            table_str += f"|{str(i).center(rank_width)}|{user_display.center(user_width)}|{str(score).center(score_width)}|\n"
            table_str += '+' + '-' * rank_width + '+' + '-' * user_width + '+' + '-' * score_width + '+\n'
            i += 1
    else:
        table_str += '|' + '目前無人上榜'.center(total_width) + '|\n'
        table_str += '+' + '-' * total_width + '+\n'
    return table_str

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
