import os
import asyncio
import sqlite3
import logging
import json
from datetime import datetime
from typing import Optional, Dict, List
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, InputFile, ContentType
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from loader import bot, dp
from aiogram import types

from environs import Env

# environs kutubxonasidan foydalanish
env = Env()
env.read_env()

# .env fayl ichidan quyidagilarni o'qiymiz
OPENAI_API_KEY = env.str("OPENAI_API_KEY")  # Bot token

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

# ==================== KONFIGURATSIYA ====================
ADMIN_ID = 1879114908
USE_OPENAI = True  # Yoqilgan

CURRENCY = "so'm"
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
ALLOWED_FILE_TYPES = ['image/jpeg', 'image/png', 'application/pdf']

# Karta ma'lumotlari
CARD_NUMBER = "8600 1234 5678 9012"
CARD_HOLDER = "JOHN DOE"

# Logging sozlash
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# OpenAI client
openai_client = None
if USE_OPENAI and OpenAI and OPENAI_API_KEY:
    openai_client = OpenAI(api_key=OPENAI_API_KEY)

# ==================== SAVOLLAR RO'YXATI ====================
QUESTIONS = [
    "1️⃣ Ismingiz?",
    "2️⃣ Loyiha nomi?",
    "3️⃣ Loyiha tavsifi (qisqacha, 2-3 jumla)?",
    "4️⃣ Qanday muammoni hal qilasiz?",
    "5️⃣ Sizning yechimingiz?",
    "6️⃣ Maqsadli auditoriya kimlar?",
    "7️⃣ Biznes model (qanday daromad olasiz)?",
    "8️⃣ Asosiy raqobatchilaringiz?",
    "9️⃣ Sizning ustunligingiz (raqobatchilardan farqi)?",
    "🔟 Moliyaviy prognoz (keyingi 1 yil)?",
]


# ==================== DATABASE ====================
class Database:
    def __init__(self, db_path='pitch_bot.db'):
        self.db_path = db_path
        self.init_db()

    def init_db(self):
        """Ma'lumotlar bazasini yaratish"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                package TEXT,
                status TEXT,
                answers TEXT,
                receipt_file_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS admin_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER,
                action TEXT,
                user_id INTEGER,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()
        conn.close()

    def save_user(self, user_id: int, username: str, full_name: str):
        """Foydalanuvchini saqlash"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO users (user_id, username, full_name)
            VALUES (?, ?, ?)
        ''', (user_id, username, full_name))
        conn.commit()
        conn.close()

    def create_order(self, user_id: int, answers: List[str], package: str) -> int:
        """Yangi buyurtma yaratish"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO orders (user_id, status, answers, package)
            VALUES (?, ?, ?, ?)
        ''', (user_id, 'awaiting_payment', '|||'.join(answers), package))
        order_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return order_id

    def update_order(self, user_id: int, **kwargs):
        """Buyurtmani yangilash"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        set_clause = ', '.join([f"{k} = ?" for k in kwargs.keys()])
        values = list(kwargs.values()) + [user_id]
        cursor.execute(f'''
            UPDATE orders 
            SET {set_clause}, updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ? AND status != 'completed'
        ''', values)
        conn.commit()
        conn.close()

    def get_order(self, user_id: int) -> Optional[Dict]:
        """Oxirgi buyurtmani olish"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, user_id, package, status, answers, receipt_file_id, created_at
            FROM orders
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT 1
        ''', (user_id,))
        row = cursor.fetchone()
        conn.close()
        if row:
            return {
                'id': row[0],
                'user_id': row[1],
                'package': row[2],
                'status': row[3],
                'answers': row[4].split('|||') if row[4] else [],
                'receipt_file_id': row[5],
                'created_at': row[6]
            }
        return None

    def log_admin_action(self, admin_id: int, action: str, user_id: int):
        """Admin harakatini loglash"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO admin_logs (admin_id, action, user_id)
            VALUES (?, ?, ?)
        ''', (admin_id, action, user_id))
        conn.commit()
        conn.close()


db = Database()


# ==================== STATES ====================
class PitchStates(StatesGroup):
    waiting_for_answer = State()
    choosing_package = State()
    waiting_for_receipt = State()


# ==================== KLAVIATURALAR ====================
def start_keyboard():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("✅ Boshlash", callback_data="start_yes"),
        InlineKeyboardButton("❌ Keyinroq", callback_data="start_no")
    )
    return kb


def package_keyboard():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton(f"🔹 Oddiy — 10,000 {CURRENCY}", callback_data="package_simple"),
        InlineKeyboardButton(f"🔸 Pro — 20,000 {CURRENCY}", callback_data="package_pro")
    )
    kb.add(InlineKeyboardButton("❌ Bekor qilish", callback_data="cancel"))
    return kb


def admin_action_kb(user_id: int):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("✅ Tasdiqlash", callback_data=f"admin_approve:{user_id}"),
        InlineKeyboardButton("❌ Rad etish", callback_data=f"admin_reject:{user_id}")
    )
    kb.add(InlineKeyboardButton("👤 Ma'lumot", callback_data=f"admin_info:{user_id}"))
    return kb


def cancel_keyboard():
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("❌ Bekor qilish", callback_data="cancel"))
    return kb


# ==================== HELPER FUNKSIYALAR ====================
async def validate_file(message: types.Message) -> tuple:
    """File validatsiyasi"""
    if message.content_type == ContentType.PHOTO:
        file_id = message.photo[-1].file_id
        file_info = await bot.get_file(file_id)
        file_size = file_info.file_size
        mime_type = 'image/jpeg'
    elif message.content_type == ContentType.DOCUMENT:
        file_id = message.document.file_id
        file_size = message.document.file_size
        mime_type = message.document.mime_type
    else:
        return False, "Faqat rasm yoki PDF yuklang", None

    if file_size > MAX_FILE_SIZE:
        max_size_mb = MAX_FILE_SIZE // 1024 // 1024
        return False, f"Fayl hajmi {max_size_mb} MB dan oshmasin", None

    if mime_type not in ALLOWED_FILE_TYPES:
        return False, "Faqat JPG, PNG yoki PDF formatda yuboring", None

    return True, "OK", file_id


def format_order_info(order: Dict) -> str:
    """Buyurtma ma'lumotlarini formatlash"""
    status_emoji = {
        'awaiting_payment': '💳',
        'receipt_sent': '⏳',
        'approved': '✅',
        'rejected': '❌',
        'completed': '🎉'
    }
    status_text = {
        'awaiting_payment': "To'lov kutilmoqda",
        'receipt_sent': 'Chek yuborildi',
        'approved': 'Tasdiqlandi',
        'rejected': 'Rad etildi',
        'completed': 'Yakunlandi'
    }
    package_text = 'Oddiy' if order.get('package') == 'simple' else 'Professional'

    status = order['status']
    emoji = status_emoji.get(status, '❓')
    text = status_text.get(status, 'Noma\'lum')
    package = package_text if order.get('package') else 'Tanlanmagan'
    created = order['created_at'][:16]

    result = f"{emoji} Holat: {text}\n"
    result += f"📦 Paket: {package}\n"
    result += f"📅 Yaratilgan: {created}"

    return result


# ==================== AI ENHANCEMENT FUNKSIYALARI ====================
async def enhance_answers_with_openai(answers: List[str], package: str) -> List[str]:
    """Javoblarni OpenAI bilan professional qilish"""
    if not USE_OPENAI or not openai_client:
        return answers

    # Pro paket uchun kuchliroq model
    model = "gpt-4" if package == "pro" else "gpt-3.5-turbo"

    try:
        enhanced_answers = []
        prompts = [
            f"Make this founder name more professional (keep it short): {answers[0]}",
            f"Create a catchy and professional project name based on: {answers[1]}",
            f"Transform this project description into a compelling elevator pitch (2-3 sentences max): {answers[2]}",
            f"Rewrite this problem statement to make it more urgent and clear for investors (3-4 bullet points): {answers[3]}",
            f"Transform this solution into clear value propositions (3-4 bullet points): {answers[4]}",
            f"Define this target audience with specific demographics and market size: {answers[5]}",
            f"Create a clear revenue model explanation with potential revenue streams: {answers[6]}",
            f"Analyze these competitors and create a competitive landscape overview: {answers[7]}",
            f"Transform these advantages into unique selling propositions (USPs): {answers[8]}",
            f"Create realistic financial projections with key metrics for next year: {answers[9]}"
        ]

        for i, prompt in enumerate(prompts):
            if i < len(answers):
                response = await asyncio.to_thread(
                    lambda: openai_client.chat.completions.create(
                        model=model,
                        messages=[
                            {"role": "system",
                             "content": "You are a professional pitch deck consultant. Keep responses concise and impactful."},
                            {"role": "user", "content": prompt}
                        ],
                        max_tokens=200,
                        temperature=0.7
                    )
                )
                enhanced_answers.append(response.choices[0].message.content.strip())
            else:
                enhanced_answers.append(answers[i] if i < len(answers) else "")

        return enhanced_answers

    except Exception as e:
        logger.error(f"OpenAI enhancement failed: {e}")
        return answers


async def create_professional_pitch_content(answers: List[str], package: str) -> Dict:
    """AI bilan to'liq pitch content yaratish"""

    if not USE_OPENAI or not openai_client:
        return {
            'project_name': answers[1] if len(answers) > 1 else "Startup",
            'author': answers[0] if len(answers) > 0 else "Entrepreneur",
            'tagline': answers[2][:100] if len(answers) > 2 else "Innovative Solution",
            'problem': answers[3] if len(answers) > 3 else "",
            'solution': answers[4] if len(answers) > 4 else "",
            'market': answers[5] if len(answers) > 5 else "",
            'business_model': answers[6] if len(answers) > 6 else "",
            'competition': answers[7] if len(answers) > 7 else "",
            'advantage': answers[8] if len(answers) > 8 else "",
            'financials': answers[9] if len(answers) > 9 else "",
            'cta': "Let's build the future together! 🚀",
        }

    model = "gpt-4" if package == "pro" else "gpt-3.5-turbo"

    prompt = f"""
Create a professional investor pitch based on these startup details. 
Make it compelling, data-driven, and investor-ready.

Founder: {answers[0] if len(answers) > 0 else ""}
Project: {answers[1] if len(answers) > 1 else ""}
Description: {answers[2] if len(answers) > 2 else ""}
Problem: {answers[3] if len(answers) > 3 else ""}
Solution: {answers[4] if len(answers) > 4 else ""}
Target Market: {answers[5] if len(answers) > 5 else ""}
Business Model: {answers[6] if len(answers) > 6 else ""}
Competition: {answers[7] if len(answers) > 7 else ""}
Our Advantage: {answers[8] if len(answers) > 8 else ""}
Financial Forecast: {answers[9] if len(answers) > 9 else ""}

Return a JSON object with these exact keys:
{{
  "project_name": "compelling project name",
  "author": "founder name",
  "tagline": "powerful tagline (max 10 words)",
  "problem": "3-4 bullet points about the problem",
  "solution": "3-4 bullet points about the solution",
  "market": "target market analysis with TAM/SAM/SOM",
  "business_model": "revenue streams and pricing strategy",
  "competition": "competitive analysis",
  "advantage": "3 key differentiators",
  "financials": "key metrics and projections",
  "cta": "powerful call to action"
}}

Make it professional and investor-ready. Use bullet points where indicated.
"""

    try:
        response = await asyncio.to_thread(
            lambda: openai_client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system",
                     "content": "You are a top-tier pitch deck consultant. Create compelling, professional content."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=1500,
                temperature=0.7,
                response_format={"type": "json_object"}
            )
        )

        content = json.loads(response.choices[0].message.content)
        return content

    except Exception as e:
        logger.error(f"AI content creation failed: {e}")
        # Fallback
        return {
            'project_name': answers[1] if len(answers) > 1 else "Startup",
            'author': answers[0] if len(answers) > 0 else "Entrepreneur",
            'tagline': answers[2][:100] if len(answers) > 2 else "Innovative Solution",
            'problem': answers[3] if len(answers) > 3 else "",
            'solution': answers[4] if len(answers) > 4 else "",
            'market': answers[5] if len(answers) > 5 else "",
            'business_model': answers[6] if len(answers) > 6 else "",
            'competition': answers[7] if len(answers) > 7 else "",
            'advantage': answers[8] if len(answers) > 8 else "",
            'financials': answers[9] if len(answers) > 9 else "",
            'cta': "Let's build the future together! 🚀",
        }


# ==================== YANGILANGAN PPTX YARATISH ====================
async def create_stunning_pitch_deck(user_id: int, answers: List[str], package: str) -> str:
    """Mukammal PPTX yaratish - AI optimized"""
    from pptx import Presentation
    from pptx.util import Inches, Pt
    from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.enum.dml import MSO_THEME_COLOR

    # AI orqali content olish
    content = await create_professional_pitch_content(answers, package)

    prs = Presentation()
    prs.slide_width = Inches(10)
    prs.slide_height = Inches(7.5)

    # Professional ranglar
    COLORS = {
        'primary': RGBColor(46, 64, 83),  # Dark blue-gray
        'secondary': RGBColor(52, 152, 219),  # Bright blue
        'accent': RGBColor(46, 204, 113),  # Green
        'danger': RGBColor(231, 76, 60),  # Red
        'warning': RGBColor(243, 156, 18),  # Orange
        'dark': RGBColor(44, 62, 80),  # Dark
        'light': RGBColor(236, 240, 241),  # Light gray
        'white': RGBColor(255, 255, 255)  # White
    }

    def add_gradient_background(slide, color1, color2):
        """Gradient fon qo'shish"""
        fill = slide.background.fill
        fill.gradient()
        fill.gradient_angle = 135
        stops = fill.gradient_stops
        stops[0].color.rgb = color1
        stops[0].position = 0.0
        stops[1].color.rgb = color2
        stops[1].position = 1.0

    # ==================== 1. TITLE SLIDE ====================
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    add_gradient_background(slide, COLORS['primary'], COLORS['secondary'])

    # Decorative shape
    shape = slide.shapes.add_shape(
        MSO_SHAPE.HEXAGON,
        Inches(7.5), Inches(0.5),
        Inches(2), Inches(2)
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = COLORS['accent']
    shape.fill.transparency = 0.7
    shape.line.fill.background()

    # Title
    title_box = slide.shapes.add_textbox(Inches(1), Inches(2), Inches(8), Inches(1.5))
    tf = title_box.text_frame
    tf.text = content['project_name'].upper()
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    p.font.name = "Arial Black"
    p.font.size = Pt(48)
    p.font.color.rgb = COLORS['white']
    p.font.bold = True

    # Tagline
    tagline_box = slide.shapes.add_textbox(Inches(1.5), Inches(3.8), Inches(7), Inches(0.8))
    tf = tagline_box.text_frame
    tf.text = content['tagline']
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    p.font.size = Pt(24)
    p.font.color.rgb = COLORS['light']
    p.font.italic = True

    # Author
    author_box = slide.shapes.add_textbox(Inches(1), Inches(6), Inches(8), Inches(0.5))
    tf = author_box.text_frame
    tf.text = f"Presented by {content['author']}"
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    p.font.size = Pt(18)
    p.font.color.rgb = COLORS['light']

    # ==================== 2. PROBLEM SLIDE ====================
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    slide.background.fill.solid()
    slide.background.fill.fore_color.rgb = COLORS['white']

    # Header bar
    header = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE,
        Inches(0), Inches(0),
        Inches(10), Inches(1.2)
    )
    header.fill.solid()
    header.fill.fore_color.rgb = COLORS['danger']
    header.line.fill.background()

    # Title
    title_box = slide.shapes.add_textbox(Inches(1), Inches(0.3), Inches(8), Inches(0.7))
    tf = title_box.text_frame
    tf.text = "🔥 THE PROBLEM"
    p = tf.paragraphs[0]
    p.font.size = Pt(36)
    p.font.color.rgb = COLORS['white']
    p.font.bold = True

    # Problem content
    content_box = slide.shapes.add_textbox(Inches(1), Inches(2), Inches(8), Inches(4.5))
    tf = content_box.text_frame
    tf.text = content['problem']
    tf.word_wrap = True
    for p in tf.paragraphs:
        p.font.size = Pt(20)
        p.font.color.rgb = COLORS['dark']
        p.space_before = Pt(12)
        p.space_after = Pt(12)

    # ==================== 3. SOLUTION SLIDE ====================
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    slide.background.fill.solid()
    slide.background.fill.fore_color.rgb = COLORS['white']

    # Header bar
    header = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE,
        Inches(0), Inches(0),
        Inches(10), Inches(1.2)
    )
    header.fill.solid()
    header.fill.fore_color.rgb = COLORS['accent']
    header.line.fill.background()

    # Title
    title_box = slide.shapes.add_textbox(Inches(1), Inches(0.3), Inches(8), Inches(0.7))
    tf = title_box.text_frame
    tf.text = "💡 OUR SOLUTION"
    p = tf.paragraphs[0]
    p.font.size = Pt(36)
    p.font.color.rgb = COLORS['white']
    p.font.bold = True

    # Solution content
    content_box = slide.shapes.add_textbox(Inches(1), Inches(2), Inches(8), Inches(4.5))
    tf = content_box.text_frame
    tf.text = content['solution']
    tf.word_wrap = True
    for p in tf.paragraphs:
        p.font.size = Pt(20)
        p.font.color.rgb = COLORS['dark']
        p.space_before = Pt(12)
        p.space_after = Pt(12)

    # ==================== 4. MARKET SLIDE ====================
    if content.get('market'):
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        slide.background.fill.solid()
        slide.background.fill.fore_color.rgb = COLORS['white']

        # Header
        header = slide.shapes.add_shape(
            MSO_SHAPE.RECTANGLE,
            Inches(0), Inches(0),
            Inches(10), Inches(1.2)
        )
        header.fill.solid()
        header.fill.fore_color.rgb = COLORS['secondary']
        header.line.fill.background()

        # Title
        title_box = slide.shapes.add_textbox(Inches(1), Inches(0.3), Inches(8), Inches(0.7))
        tf = title_box.text_frame
        tf.text = "🎯 TARGET MARKET"
        p = tf.paragraphs[0]
        p.font.size = Pt(36)
        p.font.color.rgb = COLORS['white']
        p.font.bold = True

        # Market content
        content_box = slide.shapes.add_textbox(Inches(1), Inches(2), Inches(8), Inches(4.5))
        tf = content_box.text_frame
        tf.text = content['market']
        tf.word_wrap = True
        for p in tf.paragraphs:
            p.font.size = Pt(20)
            p.font.color.rgb = COLORS['dark']
            p.space_before = Pt(12)

    # ==================== 5. BUSINESS MODEL ====================
    if content.get('business_model'):
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        slide.background.fill.solid()
        slide.background.fill.fore_color.rgb = COLORS['white']

        # Header
        header = slide.shapes.add_shape(
            MSO_SHAPE.RECTANGLE,
            Inches(0), Inches(0),
            Inches(10), Inches(1.2)
        )
        header.fill.solid()
        header.fill.fore_color.rgb = COLORS['warning']
        header.line.fill.background()

        # Title
        title_box = slide.shapes.add_textbox(Inches(1), Inches(0.3), Inches(8), Inches(0.7))
        tf = title_box.text_frame
        tf.text = "💼 BUSINESS MODEL"
        p = tf.paragraphs[0]
        p.font.size = Pt(36)
        p.font.color.rgb = COLORS['white']
        p.font.bold = True

        # Business model content
        content_box = slide.shapes.add_textbox(Inches(1), Inches(2), Inches(8), Inches(4.5))
        tf = content_box.text_frame
        tf.text = content['business_model']
        tf.word_wrap = True
        for p in tf.paragraphs:
            p.font.size = Pt(20)
            p.font.color.rgb = COLORS['dark']
            p.space_before = Pt(12)

    # ==================== 6. COMPETITIVE ADVANTAGE ====================
    if content.get('advantage'):
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        slide.background.fill.solid()
        slide.background.fill.fore_color.rgb = COLORS['white']

        # Header
        header = slide.shapes.add_shape(
            MSO_SHAPE.RECTANGLE,
            Inches(0), Inches(0),
            Inches(10), Inches(1.2)
        )
        header.fill.solid()
        header.fill.fore_color.rgb = COLORS['primary']
        header.line.fill.background()

        # Title
        title_box = slide.shapes.add_textbox(Inches(1), Inches(0.3), Inches(8), Inches(0.7))
        tf = title_box.text_frame
        tf.text = "⭐ COMPETITIVE ADVANTAGE"
        p = tf.paragraphs[0]
        p.font.size = Pt(36)
        p.font.color.rgb = COLORS['white']
        p.font.bold = True

        # Advantages
        content_box = slide.shapes.add_textbox(Inches(1), Inches(2), Inches(8), Inches(4.5))
        tf = content_box.text_frame
        tf.text = content['advantage']
        tf.word_wrap = True
        for p in tf.paragraphs:
            p.font.size = Pt(20)
            p.font.color.rgb = COLORS['dark']
            p.space_before = Pt(12)

    # ==================== 7. FINANCIALS ====================
    if content.get('financials'):
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        slide.background.fill.solid()
        slide.background.fill.fore_color.rgb = COLORS['white']

        # Header
        header = slide.shapes.add_shape(
            MSO_SHAPE.RECTANGLE,
            Inches(0), Inches(0),
            Inches(10), Inches(1.2)
        )
        header.fill.solid()
        header.fill.fore_color.rgb = COLORS['secondary']
        header.line.fill.background()

        # Title
        title_box = slide.shapes.add_textbox(Inches(1), Inches(0.3), Inches(8), Inches(0.7))
        tf = title_box.text_frame
        tf.text = "📈 FINANCIAL PROJECTIONS"
        p = tf.paragraphs[0]
        p.font.size = Pt(36)
        p.font.color.rgb = COLORS['white']
        p.font.bold = True

        # Financial content
        content_box = slide.shapes.add_textbox(Inches(1), Inches(2), Inches(8), Inches(4.5))
        tf = content_box.text_frame
        tf.text = content['financials']
        tf.word_wrap = True
        for p in tf.paragraphs:
            p.font.size = Pt(20)
            p.font.color.rgb = COLORS['dark']
            p.space_before = Pt(12)

    # ==================== 8. CALL TO ACTION ====================
    # F-STRING MUAMMOSINI HAL QILISH
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    add_gradient_background(slide, COLORS['accent'], COLORS['secondary'])

    # CTA box
    cta_box = slide.shapes.add_textbox(Inches(1.5), Inches(2), Inches(7), Inches(3.5))
    tf = cta_box.text_frame

    # F-string ichida \n ishlatmasdan text yaratish
    cta_title = "🚀 LET'S BUILD THE FUTURE"
    cta_text = content['cta']
    contact_text = f"Contact: {content['author']}"
    project_text = f"Project: {content['project_name']}"

    # Text qo'shish
    tf.text = cta_title + "\n\n"
    tf.text += cta_text + "\n\n"
    tf.text += contact_text + "\n"
    tf.text += project_text

    for p in tf.paragraphs:
        p.alignment = PP_ALIGN.CENTER
        p.font.size = Pt(26)
        p.font.color.rgb = COLORS['white']
        p.font.bold = True
        p.space_after = Pt(20)

    # Save file
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f"pitch_{package}_{user_id}_{timestamp}.pptx"
    prs.save(filename)

    logger.info(f"Created presentation: {filename}")
    return filename


# ==================== START CALLBACK HANDLERS ====================
@dp.callback_query_handler(lambda c: c.data == "start_yes", state='*')
async def start_yes_handler(call: types.CallbackQuery, state: FSMContext):
    """Boshlash tugmasi bosilganda"""
    logger.info(f"start_yes pressed by user {call.from_user.id}")
    await call.answer("Boshlaymiz! ✅")

    user_id = call.from_user.id

    # State ga birinchi savol index ni saqlash
    await state.update_data(current_question=0, answers=[])

    # Birinchi savolni berish
    text = (
        "📋 Ajoyib! Endi sizga 10 ta savol beraman.\n"
        "Har biriga javob bering.\n\n"
        f"{QUESTIONS[0]}"
    )

    await call.message.edit_text(text, reply_markup=cancel_keyboard())
    await PitchStates.waiting_for_answer.set()


@dp.callback_query_handler(lambda c: c.data == "start_no", state='*')
async def start_no_handler(call: types.CallbackQuery):
    """Keyinroq tugmasi bosilganda"""
    logger.info(f"start_no pressed by user {call.from_user.id}")
    await call.answer("Xo'sh, tayyor bo'lganingizda /start ni bosing")
    text = "👌 Mayli, tayyor bo'lganingizda /start buyrug'ini yuboring."
    await call.message.edit_text(text)


# ==================== MESSAGE HANDLER: Javoblarni qabul qilish ====================
@dp.message_handler(state=PitchStates.waiting_for_answer)
async def answer_handler(message: types.Message, state: FSMContext):
    """Javoblarni ketma-ket qabul qilish"""
    user_id = message.from_user.id
    user_data = await state.get_data()

    current_q = user_data.get('current_question', 0)
    answers = user_data.get('answers', [])

    # Javobni saqlash
    answers.append(message.text.strip())
    logger.info(f"User {user_id} answered question {current_q + 1}/{len(QUESTIONS)}")

    # Keyingi savol
    next_q = current_q + 1

    if next_q < len(QUESTIONS):
        # Yana savol bor
        await state.update_data(current_question=next_q, answers=answers)

        progress = f"✅ {next_q}/{len(QUESTIONS)} savol javoblandi\n\n"
        text = progress + QUESTIONS[next_q]

        await message.answer(text, reply_markup=cancel_keyboard())
    else:
        # Barcha savollar tugadi - paket tanlash
        await state.update_data(answers=answers)

        summary = (
            f"🎉 Barcha savollar tugadi!\n\n"
            f"📊 Jami {len(answers)} ta javob qabul qilindi.\n\n"
            f"Endi paketni tanlang:"
        )

        await message.answer(summary, reply_markup=package_keyboard())
        await PitchStates.choosing_package.set()


# ==================== PAKET TANLASH ====================
@dp.callback_query_handler(lambda c: c.data.startswith("package_"), state=PitchStates.choosing_package)
async def package_select_handler(call: types.CallbackQuery, state: FSMContext):
    """Paket tanlash"""
    logger.info(f"Package selected by user {call.from_user.id}: {call.data}")
    await call.answer()

    user_id = call.from_user.id
    user_data = await state.get_data()
    answers = user_data.get('answers', [])

    package = "simple" if call.data == "package_simple" else "pro"
    amount = 10000 if package == "simple" else 20000

    # Buyurtma yaratish
    order_id = db.create_order(user_id, answers, package)
    logger.info(f"Order {order_id} created for user {user_id}")

    package_name = 'Oddiy' if package == 'simple' else 'Professional'
    formatted_amount = f"{amount:,}"

    # Karta ma'lumotlarini ko'rsatish
    payment_text = (
        f"✅ {package_name} paket tanlandi\n\n"
        f"💰 Narx: {formatted_amount} {CURRENCY}\n\n"
        f"💳 TO'LOV MA'LUMOTLARI:\n\n"
        f"Karta raqami:\n"
        f"<code>{CARD_NUMBER}</code>\n\n"
        f"Karta egasi: {CARD_HOLDER}\n"
        f"Summa: <b>{formatted_amount} {CURRENCY}</b>\n\n"
        f"📸 To'lov qilgandan keyin chekni (skrinshot yoki PDF)\n"
        f"bu chatga yuboring.\n\n"
        f"⏳ Admin tasdiqlagach PPTX fayli yuboriladi."
    )

    await call.message.edit_text(payment_text, parse_mode="HTML", reply_markup=cancel_keyboard())
    await PitchStates.waiting_for_receipt.set()


# ==================== CHEK YUKLASH ====================
@dp.message_handler(content_types=[ContentType.PHOTO, ContentType.DOCUMENT], state=PitchStates.waiting_for_receipt)
async def receipt_handler(message: types.Message, state: FSMContext):
    """To'lov chekini qabul qilish"""
    user_id = message.from_user.id
    order = db.get_order(user_id)

    if not order:
        await message.reply("❌ Buyurtma topilmadi. Iltimos /start dan boshlang.")
        return

    # File validatsiya
    is_valid, error_msg, file_id = await validate_file(message)
    if not is_valid:
        await message.reply(f"❌ {error_msg}")
        return

    # Orderga chek file_id ni saqlash
    db.update_order(user_id, receipt_file_id=file_id, status="receipt_sent")
    logger.info(f"Receipt uploaded by user {user_id}")

    user = message.from_user
    package_name = 'Oddiy' if order['package'] == 'simple' else 'Professional'
    amount = 10000 if order['package'] == 'simple' else 20000
    formatted_amount = f"{amount:,}"
    username = user.username or 'yoq'
    current_time = datetime.now().strftime('%Y-%m-%d %H:%M')

    # Admin uchun xabar
    info_text = (
        "💳 YANGI TO'LOV CHEKI\n\n"
        "👤 Foydalanuvchi:\n"
        f"  • Ism: {user.full_name}\n"
        f"  • Username: @{username}\n"
        f"  • ID: {user_id}\n\n"
        f"📦 Paket: {package_name}\n"
        f"💰 Summa: {formatted_amount} {CURRENCY}\n\n"
        f"⏰ Vaqt: {current_time}\n"
        f"📝 Javoblar: {len(order['answers'])} ta"
    )

    # Adminga yuborish
    try:
        if message.content_type == ContentType.PHOTO:
            await bot.send_photo(
                chat_id=ADMIN_ID,
                photo=file_id,
                caption=info_text,
                reply_markup=admin_action_kb(user_id)
            )
        else:
            await bot.send_document(
                chat_id=ADMIN_ID,
                document=file_id,
                caption=info_text,
                reply_markup=admin_action_kb(user_id)
            )

        success_text = (
            "✅ Chek qabul qilindi va adminga yuborildi!\n\n"
            "⏳ Iltimos tasdiqlanishini kuting.\n"
            "Bu odatda 5-30 daqiqa vaqt oladi.\n\n"
            "Holatingizni /status buyrug'i orqali tekshirishingiz mumkin."
        )
        await message.reply(success_text)
        await state.finish()
        logger.info(f"Receipt sent to admin from user {user_id}")

    except Exception as e:
        logger.error(f"Failed to send receipt to admin: {e}")
        error_text = (
            "❌ Adminga yuborishda xatolik yuz berdi.\n"
            "Iltimos keyinroq qayta urinib ko'ring."
        )
        await message.reply(error_text)


# ==================== ADMIN ACTIONS ====================
@dp.callback_query_handler(lambda c: c.data.startswith("admin_"), state='*')
async def admin_action_handler(call: types.CallbackQuery):
    """Admin harakatlari"""
    logger.info(f"Admin action by {call.from_user.id}: {call.data}")
    await call.answer()

    if call.from_user.id != ADMIN_ID:
        await call.message.answer("⛔ Sizda ruxsat yo'q!")
        return

    parts = call.data.split(":")
    action = parts[0]
    user_id = int(parts[1])
    order = db.get_order(user_id)

    if not order:
        await call.message.answer("❌ Buyurtma topilmadi")
        return

    # Ma'lumot ko'rsatish
    if action == "admin_info":
        order_info = format_order_info(order)
        answers_count = len(order['answers'])
        order_id = order['id']

        # Javoblarni ko'rsatish
        answers_text = "\n\n📝 JAVOBLAR:\n"
        for i, ans in enumerate(order['answers'], 1):
            answers_text += f"{i}. {ans[:50]}...\n" if len(ans) > 50 else f"{i}. {ans}\n"

        info = (
            "📊 BUYURTMA TAFSILOTLARI\n\n"
            f"{order_info}\n"
            f"📝 Javoblar: {answers_count} ta\n"
            f"🆔 Buyurtma ID: {order_id}"
            f"{answers_text}"
        )
        await call.message.answer(info)
        return

    # Rad etish
    if action == "admin_reject":
        db.update_order(user_id, status="rejected")
        db.log_admin_action(ADMIN_ID, "reject", user_id)

        try:
            reject_text = (
                "❌ To'lovingiz rad etildi\n\n"
                "Sabablari:\n"
                "• Noto'g'ri summa\n"
                "• Noaniq chek\n"
                "• Boshqa muammo\n\n"
                "Iltimos to'g'ri chekni qayta yuboring yoki /start dan qaytadan boshlang."
            )
            await bot.send_message(chat_id=user_id, text=reject_text)
            await call.message.answer(f"✅ Foydalanuvchi {user_id} ga rad xabari yuborildi")
        except Exception as e:
            logger.error(f"Failed to notify user {user_id}: {e}")
            await call.message.answer(f"❌ Foydalanuvchiga xabar yuborib bo'lmadi: {e}")
        return

    # Tasdiqlash va PPTX yaratish
    if action == "admin_approve":
        db.update_order(user_id, status="approved")
        db.log_admin_action(ADMIN_ID, "approve", user_id)

        await call.message.answer("⏳ To'lov tasdiqlandi. Professional PPTX tayyorlanmoqda...")

        try:
            answers = order['answers']

            # AI bilan yaxshilash
            if USE_OPENAI and openai_client:
                await call.message.answer("🤖 AI javoblarni optimizatsiya qilmoqda...")
                enhanced_answers = await enhance_answers_with_openai(answers, order['package'])
            else:
                enhanced_answers = answers

            # Mukammal PPTX yaratish
            await call.message.answer("🎨 Professional prezentatsiya yaratilmoqda...")
            pptx_path = await create_stunning_pitch_deck(user_id, enhanced_answers, order['package'])

            # Foydalanuvchiga yuborish
            package_name = 'Oddiy' if order['package'] == 'simple' else 'Professional'

            caption = (
                "🎉 Sizning professional Pitch Deck tayyor!\n\n"
                f"📦 Paket: {package_name}\n"
                f"✨ AI optimizatsiyasi: {'✅ Qo'llanildi' if USE_OPENAI else '➖ Mavjud emas'}\n"
                f"📄 Slaydlar soni: 8-10 ta\n\n"
                "🚀 Investorlarga muvaffaqiyatlar tilaymiz!\n"
                "💡 Maslahat: Prezentatsiyani ko'rib chiqing va kerak bo'lsa tahrirlang."
            )

            with open(pptx_path, "rb") as f:
                await bot.send_document(
                    chat_id=user_id,
                    document=InputFile(f, filename=os.path.basename(pptx_path)),
                    caption=caption
                )

            db.update_order(user_id, status="completed")
            await call.message.answer(f"✅ Professional PPTX foydalanuvchi {user_id} ga yuborildi!")

            # Faylni o'chirish
            if os.path.exists(pptx_path):
                os.remove(pptx_path)
                logger.info(f"Temporary file deleted: {pptx_path}")

        except Exception as e:
            logger.error(f"PPTX generation failed: {e}")
            await call.message.answer(f"❌ Xatolik: {str(e)}")
            db.update_order(user_id, status="error")

            # Foydalanuvchiga xatolik haqida xabar
            await bot.send_message(
                chat_id=user_id,
                text="❌ Texnik xatolik yuz berdi. Admin bilan bog'laning: @support"
            )


# ==================== CANCEL HANDLER ====================
@dp.callback_query_handler(lambda c: c.data == "cancel", state='*')
async def cancel_callback_handler(call: types.CallbackQuery, state: FSMContext):
    """Bekor qilish callback"""
    logger.info(f"Cancel by user {call.from_user.id}")
    await call.answer("Jarayon bekor qilindi")

    current_state = await state.get_state()
    if current_state:
        await state.finish()

    text = (
        "❌ Jarayon bekor qilindi\n\n"
        "Qaytadan boshlash uchun /start buyrug'ini yuboring."
    )
    await call.message.edit_text(text)


@dp.message_handler(commands=['cancel'], state='*')
async def cancel_command_handler(message: types.Message, state: FSMContext):
    """Cancel buyrug'i"""
    current_state = await state.get_state()

    if current_state is None:
        await message.answer("Hozir hech qanday jarayon bajarilmayapti.")
        return

    await state.finish()

    text = (
        "❌ Jarayon bekor qilindi\n\n"
        "Qaytadan boshlash uchun /start buyrug'ini yuboring."
    )
    await message.answer(text)


# ==================== COMMAND HANDLERS ====================
@dp.message_handler(commands=['start'], state='*')
async def start_handler(message: types.Message, state: FSMContext):
    """Start command"""
    # Agar jarayon borligini tekshirish
    current_state = await state.get_state()
    if current_state:
        await state.finish()

    user = message.from_user
    db.save_user(user.id, user.username, user.full_name)
    logger.info(f"Start command by user {user.id}")

    text = (
        f"👋 Assalomu alaykum, {user.full_name}!\n\n"
        "🎯 Men sizning startup pitch'ingizni tayyorlashga yordam beraman.\n\n"
        "📝 Jarayon:\n"
        "1️⃣ 10 ta savolga javob bering\n"
        "2️⃣ Paketni tanlang (Oddiy/Pro)\n"
        "3️⃣ Kartaga to'lov qiling\n"
        "4️⃣ Chekni yuboring\n"
        "5️⃣ Professional PPTX oling\n\n"
        "Boshlaysizmi?"
    )
    await message.answer(text, reply_markup=start_keyboard())


@dp.message_handler(commands=['status'])
async def status_handler(message: types.Message):
    """Buyurtma holatini ko'rish"""
    user_id = message.from_user.id
    order = db.get_order(user_id)

    if not order:
        await message.answer(
            "❌ Buyurtma topilmadi.\n\n"
            "Yangi buyurtma yaratish uchun /start ni bosing."
        )
        return

    order_info = format_order_info(order)
    answers_count = len(order['answers'])

    # Holat asosida qo'shimcha ma'lumot
    status = order['status']
    extra_info = ""

    if status == "awaiting_payment":
        extra_info = "\n\n💳 To'lov kutilmoqda. Chekni yuboring."
    elif status == "receipt_sent":
        extra_info = "\n\n⏳ Chek adminga yuborildi. Tasdiq kutilmoqda."
    elif status == "approved":
        extra_info = "\n\n✅ Tasdiqlandi! PPTX tez orada yuboriladi."
    elif status == "rejected":
        extra_info = "\n\n❌ To'lov rad etildi. Qayta yuboring yoki /start."
    elif status == "completed":
        extra_info = "\n\n🎉 Yakunlandi! PPTX yuborilgan."

    text = (
        "📊 BUYURTMA HOLATI\n\n"
        f"{order_info}\n"
        f"📝 Javoblar: {answers_count} ta"
        f"{extra_info}"
    )
    await message.answer(text)


@dp.message_handler(commands=['help'])
async def help_handler(message: types.Message):
    """Yordam"""
    text = (
        "🆘 YORDAM\n\n"
        "📝 Buyruqlar:\n"
        "/start - Yangi pitch boshlash\n"
        "/status - Buyurtma holatini ko'rish\n"
        "/cancel - Jarayonni bekor qilish\n"
        "/help - Yordam\n\n"
        "💡 Qanday ishlaydi?\n"
        "1. /start ni bosing\n"
        "2. 10 ta savolga javob bering\n"
        "3. Paket tanlang\n"
        "4. Kartaga to'lov qiling\n"
        "5. Chekni yuboring\n"
        "6. Admin tasdiqlagach PPTX oling\n\n"
        "❓ Savol: @support"
    )
    await message.answer(text)


# ==================== ADMIN COMMANDS ====================
@dp.message_handler(commands=['admin'], user_id=ADMIN_ID)
async def admin_panel_handler(message: types.Message):
    """Admin panel"""
    conn = sqlite3.connect('pitch_bot.db')
    cursor = conn.cursor()

    # Statistika
    cursor.execute('SELECT COUNT(*) FROM orders')
    total_orders = cursor.fetchone()[0]

    cursor.execute('SELECT COUNT(*) FROM orders WHERE status = "completed"')
    completed = cursor.fetchone()[0]

    cursor.execute('SELECT COUNT(*) FROM orders WHERE status = "receipt_sent"')
    pending = cursor.fetchone()[0]

    cursor.execute('SELECT COUNT(*) FROM users')
    total_users = cursor.fetchone()[0]

    conn.close()

    text = (
        "👨‍💼 ADMIN PANEL\n\n"
        "📊 Statistika:\n"
        f"  • Jami buyurtmalar: {total_orders}\n"
        f"  • Yakunlangan: {completed}\n"
        f"  • Kutilmoqda: {pending}\n"
        f"  • Foydalanuvchilar: {total_users}\n\n"
        "Yangi chek kelganda sizga xabar beriladi."
    )
    await message.answer(text)


# ==================== NOTO'G'RI XABARLAR ====================
@dp.message_handler(state=PitchStates.waiting_for_receipt)
async def wrong_message_receipt_state(message: types.Message):
    """Receipt kutilayotganda text xabar"""
    await message.reply(
        "⚠️ Iltimos to'lov chekini (rasm yoki PDF) yuboring.\n\n"
        "Agar to'lovni bekor qilmoqchi bo'lsangiz, /cancel ni bosing."
    )


@dp.message_handler(state=PitchStates.choosing_package)
async def wrong_message_package_state(message: types.Message):
    """Paket tanlash state da text xabar"""
    await message.reply(
        "⚠️ Iltimos yuqoridagi tugmalardan paketni tanlang.\n\n"
        "Bekor qilish: /cancel"
    )


@dp.message_handler(content_types=ContentType.ANY, state='*')
async def unknown_content_handler(message: types.Message):
    """Noma'lum content type"""
    await message.reply(
        "❓ Tushunmadim.\n\n"
        "Buyruqlar:\n"
        "/start - Boshlash\n"
        "/status - Holat\n"
        "/help - Yordam"
    )


# ==================== ERROR HANDLER ====================
@dp.errors_handler()
async def errors_handler(update, exception):
    """Global error handler"""
    logger.error(f"Update {update} caused error {exception}")
    return True


# ==================== DEBUG ====================
if __name__ == '__main__':
    logger.info("=" * 50)
    logger.info("Bot ishga tushmoqda...")
    logger.info(f"Admin ID: {ADMIN_ID}")
    logger.info(f"OpenAI: {'Enabled' if USE_OPENAI else 'Disabled'}")
    logger.info(f"Database: pitch_bot.db")
    logger.info("=" * 50)