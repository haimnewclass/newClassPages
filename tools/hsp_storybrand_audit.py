#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HSP StoryBrand Landing Page Auditor
===================================
סוכן שסורק את כל קבצי ה-HTML הקשורים ל-HSP בריפו, מנתח כל דף נחיתה לפי
עקרונות StoryBrand (Donald Miller / SB7), ומפיק דוח בעברית הכולל:

  * 10 מדדים (ציון 0-10 לכל מדד) + ציון פופולריות כולל 0-100
  * ניתוח אורך: ארוך מדי / קצר מדי / תקין
  * אילו סוגי קהל יאהבו את הדף ואילו יירתעו ממנו
  * מעקב שינויים: מה נוסף / השתנה / נמחק מאז הריצה הקודמת

שימוש:
  python3 tools/hsp_storybrand_audit.py
  python3 tools/hsp_storybrand_audit.py --root . --pattern hsp \
      --report reports/hsp-storybrand-report.md

הסקריפט משתמש בספרייה הסטנדרטית בלבד (אין צורך ב-pip install).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

# ---------------------------------------------------------------------------
# חילוץ טקסט ומבנה מתוך HTML
# ---------------------------------------------------------------------------

SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "head"}
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


class PageExtractor(HTMLParser):
    """מחלץ טקסט גלוי ומאפיינים מבניים מדף HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text_parts: list[str] = []
        self.headings: list[tuple[str, str]] = []  # (tag, text)
        self.cta_texts: list[str] = []  # טקסטים של כפתורים/קישורים
        self.list_items = 0
        self.ordered_lists = 0
        self.paragraphs = 0
        self.blockquotes = 0
        self.images = 0
        self.forms = 0
        self.videos = 0
        self.title = ""
        self.meta_description = ""
        self.testimonial_blocks = 0

        self._skip_depth = 0
        self._heading_tag: str | None = None
        self._heading_buf: list[str] = []
        self._cta_depth = 0
        self._cta_buf: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS and tag != "head":
            self._skip_depth += 1
            return
        attrs_d = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            if (attrs_d.get("name") or "").lower() == "description":
                self.meta_description = attrs_d.get("content") or ""
        elif tag in HEADING_TAGS:
            self._heading_tag = tag
            self._heading_buf = []
        elif tag in ("a", "button") or (
            tag == "input" and (attrs_d.get("type") or "").lower() in ("submit", "button")
        ):
            self._cta_depth += 1
            self._cta_buf = []
            if tag == "input" and attrs_d.get("value"):
                self._cta_buf.append(attrs_d["value"])
        elif tag == "li":
            self.list_items += 1
        elif tag == "ol":
            self.ordered_lists += 1
        elif tag == "p":
            self.paragraphs += 1
        elif tag == "blockquote":
            self.blockquotes += 1
        elif tag == "img":
            self.images += 1
        elif tag == "form":
            self.forms += 1
        elif tag in ("video", "iframe"):
            self.videos += 1

        cls = (attrs_d.get("class") or "") + " " + (attrs_d.get("id") or "")
        if re.search(r"testimonial|review|quote|recommendation|המלצ", cls, re.I):
            self.testimonial_blocks += 1

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS and tag != "head":
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "title":
            self._in_title = False
        elif tag in HEADING_TAGS and self._heading_tag == tag:
            text = " ".join(self._heading_buf).strip()
            if text:
                self.headings.append((tag, text))
            self._heading_tag = None
        elif tag in ("a", "button") and self._cta_depth > 0:
            self._cta_depth -= 1
            text = " ".join(self._cta_buf).strip()
            if text:
                self.cta_texts.append(text)

    def handle_data(self, data):
        if self._skip_depth > 0:
            return
        text = data.strip()
        if not text:
            return
        if self._in_title:
            self.title += text
        if self._heading_tag:
            self._heading_buf.append(text)
        if self._cta_depth > 0:
            self._cta_buf.append(text)
        self.text_parts.append(text)

    @property
    def full_text(self) -> str:
        return " ".join(self.text_parts)


# ---------------------------------------------------------------------------
# מילוני מילות מפתח (עברית + אנגלית)
# ---------------------------------------------------------------------------

KW = {
    "customer_address": [
        "אתה", "את ", "אתם", "אתן", "לך", "לכם", "לכן", "שלך", "שלכם", "עבורך",
        "עבורכם", "בשבילך", "בשבילכם", "תרצו", "תרצה", "מגיע לך",
        "you", "your", "you're", "yours",
    ],
    "desire": [
        "רוצה", "רוצים", "חולם", "חולמים", "שואף", "מטרה", "המטרה שלך",
        "מחפש", "מחפשים", "מגיע לכם",
        "want", "dream", "goal", "looking for", "deserve",
    ],
    "problem": [
        "בעיה", "בעיות", "קושי", "קשיים", "מתקשה", "מתקשים", "כאב", "תסכול",
        "מתוסכל", "נמאס", "מאבק", "נאבק", "לחץ", "עומס", "בלבול", "מבולבל",
        "חרדה", "פחד", "דאגה", "מודאג", "לא מצליח", "תקוע", "תקועים", "אתגר",
        "problem", "struggle", "struggling", "pain", "frustrat", "overwhelm",
        "stress", "stuck", "confus", "worry", "worried", "fear", "challenge",
    ],
    "internal_problem": [
        "מרגיש", "מרגישה", "מרגישים", "תחושה", "תחושת", "ביטחון", "בטוח בעצמ",
        "ערך עצמי", "לבד", "בדידות", "אשמה", "בושה", "חוסר אונים",
        "feel", "feeling", "confidence", "alone", "guilt", "shame",
    ],
    "empathy": [
        "אנחנו מבינים", "אנו מבינים", "מבינים אותך", "מבינים אתכם", "גם אנחנו",
        "אנחנו יודעים", "אנו יודעים", "עברנו את זה", "מכירים את זה",
        "בדיוק בשביל זה", "אתה לא לבד", "את לא לבד", "אתם לא לבדים",
        "we understand", "we know", "we get it", "you're not alone",
    ],
    "authority": [
        "ניסיון", "שנות ניסיון", "שנים של", "מומחה", "מומחים", "מומחית",
        "מוסמך", "מוסמכת", "הוסמכ", "תעודה", "תואר", "דוקטור", 'ד"ר',
        "פרופסור", "ליווינו", "עזרנו", "אלפי", "מאות", "לקוחות מרוצים",
        "בוגרים", "הצלחות מוכחות", "שיטה מוכחת",
        "years of experience", "expert", "certified", "phd", "proven",
        "trusted by", "clients", "graduates",
    ],
    "plan": [
        "שלב", "שלבים", "שלב ראשון", "שלב שני", "שלב שלישי", "התהליך",
        "איך זה עובד", "כך זה עובד", "התוכנית", "המסלול", "פשוט:",
        "step", "steps", "how it works", "the plan", "the process", "phase",
    ],
    "cta": [
        "הרשמה", "הירשם", "הירשמי", "הירשמו", "להרשמה", "השאר פרטים",
        "השאירו פרטים", "צור קשר", "צרו קשר", "התקשר", "התקשרו", "חייג",
        "לחץ כאן", "לחצו כאן", "התחל", "התחילו", "הצטרף", "הצטרפו", "קבע",
        "קבעו", "הזמן", "הזמינו", "לתיאום", "לקביעת", "שיחת ייעוץ", "אבחון",
        "הורד", "הורידו", "קבל", "קבלו", "לפרטים", "אני רוצה", "בואו נתחיל",
        "דברו איתי", "שלחו לי", "וואטסאפ",
        "sign up", "register", "contact", "call now", "click here", "start",
        "join", "book", "schedule", "download", "get started", "free consult",
        "whatsapp",
    ],
    "transitional_cta": [
        "מדריך חינם", "בחינם", "ללא עלות", "ללא התחייבות", "שיעור ניסיון",
        "פגישת היכרות", "אבחון חינם", "סרטון חינם", "וובינר",
        "free guide", "free trial", "no obligation", "free lesson", "webinar",
        "download free",
    ],
    "stakes": [
        "אל תפספס", "אל תפספסו", "אל תישאר", "אל תישארו", "לפני שיהיה מאוחר",
        "מה יקרה אם", "בלי זה", "ימשיך", "יישאר מאחור", "מפסיד", "מפסידים",
        "להפסיד", "הפער", "יגדל", "אל תוותרו", "אל תיתן", "לא כדאי לחכות",
        "don't miss", "don't wait", "left behind", "lose", "losing", "miss out",
        "before it's too late", "risk",
    ],
    "success": [
        "הצלחה", "מצליח", "מצליחה", "מצליחים", "תוצאות", "שיפור", "לשפר",
        "להשתפר", "תדמיין", "תדמיינו", "דמיינו", "עתיד", "השינוי", "שינוי אמיתי",
        "ביטחון עצמי", "שליטה", "שקט נפשי", "גאווה", "גאה", "חיוך", "פריחה",
        "לפרוח", "להתקדם", "התקדמות", "הישג", "הישגים", "מומחיות", "שליטה מלאה",
        "success", "succeed", "results", "improve", "imagine", "transform",
        "confidence", "thrive", "achieve", "progress", "master",
    ],
    "social_proof": [
        "המלצה", "המלצות", "ממליץ", "ממליצה", "ממליצים", "מרוצים", "סיפורי הצלחה",
        "ביקורות", "דירוג", "כוכבים", "מה אומרים", "לקוחות מספרים", "הורים מספרים",
        "testimonial", "review", "reviews", "rating", "stars", "what people say",
        "success stories",
    ],
    "guarantee": [
        "אחריות", "החזר כספי", "התחייבות", "ללא סיכון", "מובטח", "מבטיחים",
        "guarantee", "money back", "risk-free", "refund",
    ],
    "urgency": [
        "עכשיו", "היום", "מיד", "מקומות אחרונים", "מקום אחרון", "מוגבל",
        "מוגבלת", "נותרו", "רק עוד", "ההרשמה נסגרת", "לזמן מוגבל", "דקות",
        "אל תחכה", "אל תחכו", "מהרו",
        "now", "today", "limited", "last spots", "only", "hurry", "closing soon",
    ],
    "price": [
        "₪", "$", "מחיר", "עלות", "הנחה", "מבצע", "תשלומים", "חינם", "בחינם",
        "price", "cost", "discount", "sale", "payment", "free",
    ],
    "story": [
        "סיפור", "הסיפור של", "פעם", "הכירו את", "כשהתחלנו", "המסע",
        "story", "journey", "meet ",
    ],
    "jargon": [
        "סינרגיה", "אופטימיזציה", "מתודולוגיה", "פרדיגמה", "הוליסטי",
        "אינטגרטיבי", "פלטפורמה", "דיגיטלי מתקדם",
        "synergy", "optimization", "methodology", "paradigm", "holistic",
        "leverage", "cutting-edge", "state-of-the-art",
    ],
    "hype": [
        "מדהים", "מהפכני", "הטוב ביותר", "הכי טוב", "פשוט וקל", "בקלות",
        "ללא מאמץ", "תוך ימים", "מיידי", "קסם",
        "amazing", "revolutionary", "the best", "effortless", "instantly",
        "magic", "incredible", "unbelievable",
    ],
}

HEB_RE = re.compile(r"[\u0590-\u05FF]")


def count_hits(text: str, keys: list[str]) -> int:
    low = text.lower()
    return sum(low.count(k.lower()) for k in keys)


def found_terms(text: str, keys: list[str], limit: int = 4) -> list[str]:
    low = text.lower()
    hits = [k for k in keys if k.lower() in low]
    return hits[:limit]


def clamp(v: float, lo: float = 0.0, hi: float = 10.0) -> float:
    return max(lo, min(hi, v))


# ---------------------------------------------------------------------------
# ניתוח דף בודד
# ---------------------------------------------------------------------------

@dataclass
class Metric:
    name: str
    score: float  # 0-10
    weight: float
    note: str


@dataclass
class PageAnalysis:
    path: str
    sha256: str
    title: str
    word_count: int
    reading_minutes: float
    length_verdict: str
    length_note: str
    metrics: list[Metric] = field(default_factory=list)
    popularity: float = 0.0  # 0-100
    verdict: str = ""
    audiences_love: list[tuple[str, str]] = field(default_factory=list)
    audiences_repelled: list[tuple[str, str]] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    error: str = ""


def analyze_page(path: Path, root: Path) -> PageAnalysis:
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    rel = str(path.relative_to(root))
    try:
        html = raw.decode("utf-8", errors="replace")
        ex = PageExtractor()
        ex.feed(html)
    except Exception as e:  # דף פגום לא אמור להפיל את כל הדוח
        return PageAnalysis(
            path=rel, sha256=sha, title="", word_count=0, reading_minutes=0,
            length_verdict="שגיאה", length_note="", error=f"שגיאה בפענוח HTML: {e}",
        )

    text = ex.full_text
    words = re.findall(r"[\w\u0590-\u05FF']+", text)
    wc = len(words)
    heading_text = " ".join(h[1] for h in ex.headings)
    h1s = [h[1] for h in ex.headings if h[0] == "h1"]
    cta_text = " ".join(ex.cta_texts)
    reading_minutes = round(wc / 200.0, 1)  # ~200 מילים לדקה בקריאת עברית

    def density(n: int) -> float:
        """נרמול מספר מופעים לפי אורך הדף (מופעים ל-500 מילים)."""
        return n * 500.0 / max(wc, 150)

    metrics: list[Metric] = []

    # --- מדד 1: הלקוח הוא הגיבור -------------------------------------------
    addr = count_hits(text, KW["customer_address"])
    desire = count_hits(text, KW["desire"])
    s1 = clamp(2 + density(addr) * 0.8 + min(desire, 4) * 0.8)
    n1 = (
        f"פניות ישירות לקורא: {addr}, ביטויי רצון/שאיפה: {desire}. "
        + ("הדף ממוקד בלקוח." if s1 >= 7 else
           "מומלץ להרבות בפנייה ישירה ('אתם', 'שלכם') ובמה שהלקוח רוצה להשיג."
           if s1 < 5.5 else "מיקוד סביר בלקוח, אפשר לחדד.")
    )
    metrics.append(Metric("דמות (הלקוח כגיבור)", s1, 1.2, n1))

    # --- מדד 2: הבעיה ---------------------------------------------------------
    prob = count_hits(text, KW["problem"])
    internal = count_hits(text, KW["internal_problem"])
    s2 = clamp(1.5 + density(prob) * 1.1 + min(density(internal), 3) * 0.9)
    terms = found_terms(text, KW["problem"])
    n2 = (
        f"אזכורי בעיה חיצונית: {prob}, בעיה פנימית/רגשית: {internal}"
        + (f" (למשל: {', '.join(terms)})." if terms else ".")
        + (" הבעיה מוגדרת היטב." if s2 >= 7 else
           " הדף כמעט לא נוגע בכאב של הלקוח — בלי בעיה אין סיפור."
           if s2 < 4 else " כדאי להעמיק גם בבעיה הרגשית, לא רק המעשית.")
    )
    metrics.append(Metric("בעיה (חיצונית + פנימית)", s2, 1.1, n2))

    # --- מדד 3: המותג כמדריך (אמפתיה + סמכות) --------------------------------
    emp = count_hits(text, KW["empathy"])
    auth = count_hits(text, KW["authority"])
    s3 = clamp(1.5 + min(emp, 3) * 1.6 + min(density(auth), 4) * 0.9)
    n3 = (
        f"ביטויי אמפתיה: {emp}, אותות סמכות (ניסיון/הסמכות/מספרים): {auth}. "
        + ("שילוב טוב של אמפתיה וסמכות." if s3 >= 7 else
           ("חסרה אמפתיה מפורשת ('אנחנו מבינים אתכם') — סמכות בלי אמפתיה מרתיעה."
            if emp == 0 and auth > 0 else
            "חסרים אותות סמכות (שנות ניסיון, כמות לקוחות, הסמכות)."
            if auth == 0 else "כדאי לחזק גם אמפתיה וגם סמכות."))
    )
    metrics.append(Metric("מדריך (אמפתיה + סמכות)", s3, 1.0, n3))

    # --- מדד 4: תוכנית ברורה --------------------------------------------------
    plan = count_hits(text, KW["plan"])
    numbered = len(re.findall(r"(?:^|\s)[123][\.\)]\s", text))
    s4 = clamp(1.5 + min(plan, 5) * 1.1 + (2.0 if ex.ordered_lists else 0)
               + min(numbered, 3) * 0.8)
    n4 = (
        f"אזכורי שלבים/תהליך: {plan}, רשימות ממוספרות: {ex.ordered_lists}. "
        + ("יש תוכנית פעולה ברורה." if s4 >= 7 else
           "מומלץ להוסיף תוכנית של 3 שלבים פשוטים ('איך זה עובד: 1..2..3').")
    )
    metrics.append(Metric("תוכנית (3 שלבים)", s4, 1.0, n4))

    # --- מדד 5: קריאה לפעולה --------------------------------------------------
    cta_hits = count_hits(cta_text + " " + text, KW["cta"])
    cta_buttons = sum(1 for t in ex.cta_texts if count_hits(t, KW["cta"]) > 0)
    trans = count_hits(text, KW["transitional_cta"])
    s5 = clamp(1 + min(cta_buttons, 4) * 1.7 + min(trans, 2) * 0.8
               + (1.0 if ex.forms else 0) + min(density(cta_hits), 3) * 0.4)
    n5 = (
        f"כפתורי/קישורי CTA: {cta_buttons}, טפסים: {ex.forms}, "
        f"הצעות מעבר רכות (חינם/ללא התחייבות): {trans}. "
        + ("קריאה לפעולה חזקה ונוכחת." if s5 >= 7 else
           "אין קריאה לפעולה ברורה — חובה כפתור בולט שחוזר לאורך הדף."
           if cta_buttons == 0 else
           "כדאי לחזור על ה-CTA מספר פעמים ולהוסיף הצעת מעבר רכה (מדריך חינם וכו').")
    )
    metrics.append(Metric("קריאה לפעולה (CTA)", s5, 1.3, n5))

    # --- מדד 6: מה מונחת על הכף (הימנעות מכישלון) ----------------------------
    stakes = count_hits(text, KW["stakes"])
    s6 = clamp(2 + min(stakes, 5) * 1.6)
    n6 = (
        f"אזכורי מחיר אי-הפעולה: {stakes}. "
        + ("ברור מה הלקוח מפסיד אם לא יפעל." if s6 >= 7 else
           "לא ברור מה קורה אם הלקוח לא פועל — משפט-שניים על המחיר של חוסר מעש מוסיפים דחיפות."
           if s6 < 5 else "יש רמז לסיכון, אפשר לחדד בעדינות (בלי להפחיד יתר על המידה).")
    )
    metrics.append(Metric("מה על הכף (כישלון נמנע)", s6, 0.8, n6))

    # --- מדד 7: חזון הצלחה ------------------------------------------------------
    succ = count_hits(text, KW["success"])
    s7 = clamp(1.5 + density(succ) * 1.0)
    n7 = (
        f"ביטויי הצלחה/שינוי חיובי: {succ}. "
        + ("תמונת ההצלחה חיה וברורה." if s7 >= 7 else
           "כדאי לצייר תמונה קונקרטית של 'איך ייראו החיים אחרי' (תוצאות, רגש, ביטחון).")
    )
    metrics.append(Metric("חזון הצלחה (טרנספורמציה)", s7, 1.0, n7))

    # --- מדד 8: בהירות המסר -----------------------------------------------------
    jargon = count_hits(text, KW["jargon"])
    h1_ok = bool(h1s) and 3 <= len(h1s[0].split()) <= 14
    head_kw = count_hits(heading_text, KW["problem"] + KW["success"] + KW["desire"] + KW["cta"])
    s8 = clamp(3 + (2.5 if h1_ok else 0) + (1.5 if len(h1s) == 1 else 0)
               + min(head_kw, 4) * 0.8 - min(jargon, 4) * 1.0
               + (1.0 if ex.meta_description else 0))
    h1_display = ('"' + h1s[0] + '"') if h1s else "חסרה h1!"
    n8 = (
        f"כותרת ראשית: {h1_display}; "
        f"ז'רגון מקצועי: {jargon}. "
        + ("מסר חד — עוברים את 'מבחן הנהמה' (Grunt Test)." if s8 >= 7 else
           "המסר לא מספיק חד: תוך 5 שניות צריך להבין מה מציעים, איך זה משפר חיים, ואיך קונים.")
    )
    metrics.append(Metric("בהירות המסר (Grunt Test)", s8, 1.2, n8))

    # --- מדד 9: אורך וסריקות -----------------------------------------------------
    if wc < 250:
        len_score, length_verdict = 3.5, "קצר מדי"
        length_note = (f"{wc} מילים בלבד — אין מספיק מקום לבנות אמון, בעיה ופתרון. "
                       "מומלץ 500–1,200 מילים לדף נחיתה בשיטת StoryBrand.")
    elif wc < 450:
        len_score, length_verdict = 6.5, "קצר (גבולי)"
        length_note = f"{wc} מילים — עובד לקהל חם, אך לקהל קר כדאי להרחיב את חלקי הבעיה וההוכחה."
    elif wc <= 1300:
        len_score, length_verdict = 9.5, "תקין"
        length_note = f"{wc} מילים — טווח אידיאלי לדף נחיתה ({reading_minutes} דק' קריאה)."
    elif wc <= 2000:
        len_score, length_verdict = 6.0, "ארוך (גבולי)"
        length_note = (f"{wc} מילים — מתחיל להיות ארוך ({reading_minutes} דק'). "
                       "ודאו שכל מקטע מרוויח את מקומו וש-CTA מופיע כבר למעלה.")
    else:
        len_score, length_verdict = 3.0, "ארוך מדי"
        length_note = (f"{wc} מילים ({reading_minutes} דק' קריאה) — רוב הגולשים ינטשו. "
                       "קצצו לפחות שליש והעבירו תוכן משני לעמוד/מדריך נפרד.")
    scan_units = len(ex.headings) + ex.list_items
    scan_density = scan_units * 500.0 / max(wc, 150)
    scan_bonus = 0.5 if scan_density >= 6 else (-1.5 if scan_density < 2 and wc > 400 else 0)
    s9 = clamp(len_score + scan_bonus)
    n9 = (length_note + f" סריקוּת: {len(ex.headings)} כותרות ו-{ex.list_items} סעיפי רשימה"
          + (" — קל לסרוק." if scan_density >= 4 else " — כדאי לשבור טקסט לכותרות ורשימות."))
    metrics.append(Metric("אורך וסריקוּת", s9, 1.0, n9))

    # --- מדד 10: אמון והוכחה חברתית ----------------------------------------------
    proof = count_hits(text, KW["social_proof"])
    guar = count_hits(text, KW["guarantee"])
    testi = ex.testimonial_blocks + ex.blockquotes
    stats = len(re.findall(r"\d+\s*%|\b\d{2,}(?:,\d{3})*\+?\b", text))
    s10 = clamp(1 + min(testi, 3) * 1.5 + min(proof, 4) * 0.7
                + min(guar, 2) * 1.2 + min(stats, 5) * 0.4)
    n10 = (
        f"בלוקים של המלצות/ציטוטים: {testi}, אזכורי הוכחה חברתית: {proof}, "
        f"אחריות/הבטחה: {guar}, נתונים מספריים: {stats}. "
        + ("בסיס אמון מצוין." if s10 >= 7 else
           "חסרה הוכחה חברתית — 2–3 המלצות אמיתיות עם שם ותמונה מזניקות המרות.")
    )
    metrics.append(Metric("אמון והוכחה חברתית", s10, 1.1, n10))

    # --- ציון פופולריות כולל -------------------------------------------------------
    total_w = sum(m.weight for m in metrics)
    popularity = round(sum(m.score * m.weight for m in metrics) / total_w * 10, 1)
    if popularity >= 80:
        verdict = "פוטנציאל פופולריות גבוה — הדף בנוי היטב לפי StoryBrand"
    elif popularity >= 65:
        verdict = "פוטנציאל בינוני-גבוה — בסיס טוב עם כמה חורים בסיפור"
    elif popularity >= 50:
        verdict = "פוטנציאל בינוני — הסיפור קיים חלקית, נדרש חיזוק ממוקד"
    else:
        verdict = "פוטנציאל נמוך — הדף לא מספר סיפור שלם וצפוי להמיר חלש"

    # --- ניתוח קהלים ----------------------------------------------------------------
    urg = count_hits(text, KW["urgency"])
    hype = count_hits(text, KW["hype"])
    price = count_hits(text, KW["price"])
    story = count_hits(text, KW["story"])
    emotion = internal + succ

    personas: list[tuple[str, float, str, str]] = []
    # (שם, ציון התאמה 0-10, למה יאהבו, למה יירתעו)

    p = clamp(5 + min(urg, 4) * 0.7 + (1.5 if wc < 700 else -1.0 if wc > 1500 else 0)
              + min(cta_buttons, 3) * 0.5)
    personas.append((
        "מחליטים מהירים ואימפולסיביים", p,
        "דחיפות, CTA בולט ודף שלא מסרבל אותם",
        "דף ארוך ואיטי בלי כפתור פעולה מיידי",
    ))

    p = clamp(5 + min(stats, 5) * 0.6 + min(auth, 5) * 0.4 - min(hype, 5) * 0.9
              + (0.8 if wc > 600 else -0.8))
    personas.append((
        "אנליטיים ומונעי נתונים", p,
        "מספרים, פירוט תהליך ואותות מקצועיות",
        "סופרלטיבים והבטחות גדולות בלי נתונים תומכים",
    ))

    p = clamp(4 + min(testi, 3) * 1.0 + min(guar, 2) * 1.2 + min(proof, 4) * 0.5
              - min(hype, 5) * 0.7 - min(urg, 5) * 0.3)
    personas.append((
        "סקפטיים וזהירים", p,
        "המלצות אמיתיות, אחריות והבטחות מגובות",
        "לחץ מכירתי ('רק היום!') בלי הוכחות",
    ))

    p = clamp(4 + min(story, 3) * 0.9 + min(emotion, 8) * 0.45 + min(emp, 3) * 0.8)
    personas.append((
        "רגשיים ומונעי סיפור", p,
        "שפה רגשית, אמפתיה וסיפור טרנספורמציה",
        "טקסט טכני ויבש שלא נוגע ברגש",
    ))

    p = clamp(4 + (2.5 if scan_density >= 5 else 0) + (1.5 if wc <= 900 else -1.5 if wc > 1600 else 0)
              + min(cta_buttons, 3) * 0.4)
    personas.append((
        "עסוקים וסורקים (מובייל)", p,
        "כותרות ורשימות שמאפשרות לסרוק ב-30 שניות",
        "גושי טקסט ארוכים בלי עוגנים ויזואליים",
    ))

    p = clamp(4.5 + min(price, 4) * 0.8 + min(trans, 2) * 1.0 + min(guar, 2) * 0.6)
    personas.append((
        "רגישים למחיר ומחפשי ערך", p,
        "שקיפות במחיר, הצעת חינם/ניסיון וללא התחייבות",
        "הסתרת מחיר מוחלטת ותחושת 'יקר ומחייב'",
    ))

    audiences_love = [(name, why) for name, sc, why, _ in personas if sc >= 6.5]
    audiences_repelled = [(name, why_not) for name, sc, _, why_not in personas if sc <= 4.5]
    if not audiences_love:
        best = max(personas, key=lambda t: t[1])
        audiences_love = [(best[0] + " (התאמה חלקית בלבד)", best[2])]
    if not audiences_repelled:
        worst = min(personas, key=lambda t: t[1])
        if worst[1] < 6.5:
            audiences_repelled = [(worst[0] + " (הסתייגות קלה)", worst[3])]

    # --- המלצות ----------------------------------------------------------------------
    recs = [f"**{m.name}** (ציון {m.score:.1f}): {m.note.split('. ', 1)[-1]}"
            for m in sorted(metrics, key=lambda m: m.score)[:3] if m.score < 7]

    return PageAnalysis(
        path=rel, sha256=sha, title=ex.title or (h1s[0] if h1s else "(ללא כותרת)"),
        word_count=wc, reading_minutes=reading_minutes,
        length_verdict=length_verdict, length_note=length_note,
        metrics=metrics, popularity=popularity, verdict=verdict,
        audiences_love=audiences_love, audiences_repelled=audiences_repelled,
        recommendations=recs,
    )


# ---------------------------------------------------------------------------
# מעקב שינויים בין ריצות
# ---------------------------------------------------------------------------

def load_state(state_path: Path) -> dict:
    if state_path.exists():
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"files": {}}


def diff_state(old: dict, pages: list[PageAnalysis]) -> dict:
    old_files: dict = old.get("files", {})
    current = {p.path: p for p in pages}
    added = sorted(set(current) - set(old_files))
    removed = sorted(set(old_files) - set(current))
    changed = []
    for path, page in sorted(current.items()):
        prev = old_files.get(path)
        if prev and prev.get("sha256") != page.sha256:
            changed.append({
                "path": path,
                "old_score": prev.get("popularity"),
                "new_score": page.popularity,
            })
    return {"added": added, "removed": removed, "changed": changed}


def save_state(state_path: Path, pages: list[PageAnalysis]) -> None:
    state = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": {p.path: {"sha256": p.sha256, "popularity": p.popularity} for p in pages},
    }
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# הפקת דוח Markdown
# ---------------------------------------------------------------------------

def score_bar(score: float, out_of: float = 10) -> str:
    filled = round(score / out_of * 10)
    return "█" * filled + "░" * (10 - filled)


def render_report(pages: list[PageAnalysis], changes: dict, pattern: str) -> str:
    now = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    lines: list[str] = []
    lines.append('<div dir="rtl">\n')
    lines.append("# דוח StoryBrand — דפי נחיתה HSP")
    lines.append(f"\n*נוצר אוטומטית בתאריך {now} | תבנית חיפוש: `*{pattern}*.html`*\n")

    # --- מה השתנה ---
    lines.append("## מה השתנה מאז הריצה הקודמת\n")
    if not (changes["added"] or changes["removed"] or changes["changed"]):
        lines.append("לא זוהו שינויים בקבצי ה-HSP (או שזו ריצה ראשונה ללא היסטוריה).\n")
    else:
        for path in changes["added"]:
            lines.append(f"- 🆕 **נוסף:** `{path}`")
        for ch in changes["changed"]:
            old_s = ch["old_score"]
            delta = ""
            if isinstance(old_s, (int, float)):
                d = round(ch["new_score"] - old_s, 1)
                arrow = "⬆️" if d > 0 else ("⬇️" if d < 0 else "↔️")
                delta = f" — ציון {old_s} ← {ch['new_score']} {arrow}"
            lines.append(f"- ✏️ **השתנה:** `{ch['path']}`{delta}")
        for path in changes["removed"]:
            lines.append(f"- 🗑️ **נמחק:** `{path}`")
        lines.append("")

    # --- סיכום כללי ---
    ok_pages = [p for p in pages if not p.error]
    lines.append("## סיכום כללי\n")
    if not pages:
        lines.append(f"לא נמצאו קבצי HTML התואמים לתבנית `*{pattern}*.html` בריפו.\n")
        lines.append("ברגע שיתווסף דף נחיתה של HSP — הוא ינותח אוטומטית בריצה הבאה.\n")
    else:
        lines.append("| דף | כותרת | ציון פופולריות | אורך | מסקנה |")
        lines.append("|---|---|---|---|---|")
        for p in sorted(ok_pages, key=lambda x: -x.popularity):
            lines.append(
                f"| `{p.path}` | {p.title[:40] or '—'} | **{p.popularity}/100** "
                f"| {p.length_verdict} ({p.word_count} מילים) | {p.verdict.split(' — ')[0]} |"
            )
        lines.append("")

    # --- פירוט לכל דף ---
    for p in ok_pages:
        lines.append("---\n")
        lines.append(f"## 📄 `{p.path}`\n")
        lines.append(f"**כותרת הדף:** {p.title or '—'}\n")
        lines.append(f"### ציון פופולריות כולל: {p.popularity}/100\n")
        lines.append(f"`{score_bar(p.popularity, 100)}`\n")
        lines.append(f"**{p.verdict}**\n")

        lines.append("### 10 מדדי StoryBrand\n")
        lines.append("| # | מדד | ציון | | הערות |")
        lines.append("|---|---|---|---|---|")
        for i, m in enumerate(p.metrics, 1):
            lines.append(f"| {i} | {m.name} | **{m.score:.1f}/10** | `{score_bar(m.score)}` | {m.note} |")
        lines.append("")

        lines.append("### אורך הדף\n")
        emoji = {"תקין": "✅", "קצר (גבולי)": "⚠️", "ארוך (גבולי)": "⚠️"}.get(p.length_verdict, "❌")
        lines.append(f"{emoji} **{p.length_verdict}** — {p.length_note}\n")

        lines.append("### מי יאהב את הדף 💚\n")
        for name, why in p.audiences_love:
            lines.append(f"- **{name}** — {why}")
        lines.append("")
        lines.append("### מי עלול להירתע 💔\n")
        if p.audiences_repelled:
            for name, why in p.audiences_repelled:
                lines.append(f"- **{name}** — {why}")
        else:
            lines.append("- לא זוהה קהל שצפוי להירתע משמעותית — הדף מאוזן היטב.")
        lines.append("")

        if p.recommendations:
            lines.append("### 3 השיפורים החשובים ביותר\n")
            for i, r in enumerate(p.recommendations, 1):
                lines.append(f"{i}. {r}")
            lines.append("")

    for p in pages:
        if p.error:
            lines.append(f"\n> ⚠️ `{p.path}` — {p.error}\n")

    lines.append("\n---\n")
    lines.append("*הדוח מבוסס על ניתוח היוריסטי של עקרונות SB7 (דמות, בעיה, מדריך, תוכנית, "
                 "קריאה לפעולה, כישלון נמנע, הצלחה) בתוספת מדדי אורך, בהירות ואמון. "
                 "הציונים הם אינדיקציה להכוונת שיפורים — לא תחליף לבדיקת A/B אמיתית.*")
    lines.append("\n</div>")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def find_pages(root: Path, pattern: str) -> list[Path]:
    pat = pattern.lower()
    results = []
    for path in root.rglob("*.html"):
        rel = path.relative_to(root)
        parts_low = str(rel).lower()
        if any(seg in (".git", "node_modules", "reports") for seg in rel.parts):
            continue
        if pat in parts_low:
            results.append(path)
    return sorted(results)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="HSP StoryBrand landing page auditor")
    ap.add_argument("--root", default=".", help="תיקיית השורש לסריקה")
    ap.add_argument("--pattern", default="hsp",
                    help="מחרוזת שחייבת להופיע בנתיב הקובץ (ברירת מחדל: hsp)")
    ap.add_argument("--report", default="reports/hsp-storybrand-report.md",
                    help="נתיב קובץ הדוח")
    ap.add_argument("--state", default="reports/.hsp_audit_state.json",
                    help="קובץ מצב למעקב שינויים בין ריצות")
    ap.add_argument("--summary-file", default=os.environ.get("GITHUB_STEP_SUMMARY"),
                    help="קובץ נוסף לכתיבת הדוח (למשל GITHUB_STEP_SUMMARY)")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    paths = find_pages(root, args.pattern)
    pages = [analyze_page(p, root) for p in paths]

    state_path = Path(args.state)
    old_state = load_state(state_path)
    changes = diff_state(old_state, pages)

    report = render_report(pages, changes, args.pattern)
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")
    save_state(state_path, pages)

    if args.summary_file:
        try:
            with open(args.summary_file, "a", encoding="utf-8") as f:
                f.write(report)
        except OSError as e:
            print(f"אזהרה: כתיבה ל-summary נכשלה: {e}", file=sys.stderr)

    print(f"נותחו {len(pages)} דפי HSP. הדוח נכתב אל: {report_path}")
    for p in pages:
        if p.error:
            print(f"  ⚠️ {p.path}: {p.error}")
        else:
            print(f"  • {p.path}: {p.popularity}/100 ({p.length_verdict}, {p.word_count} מילים)")
    if changes["added"] or changes["changed"] or changes["removed"]:
        print(f"שינויים: {len(changes['added'])} נוספו, "
              f"{len(changes['changed'])} השתנו, {len(changes['removed'])} נמחקו")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
