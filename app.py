# Cell 2: Astra Ultimate (Smart Reset Edition)
%%writefile app.py
import streamlit as st
import json
import re
import io
import time
import ast
import uuid
from google import genai
from google.genai import types
from groq import Groq
from docx import Document
from docx.shared import Pt, Inches
from docx.enum.text import WD_PARAGRAPH_ALIGNMENT
from docx.oxml.ns import qn

# --- PDF LIBRARIES ---
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, ListFlowable, ListItem
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_JUSTIFY, TA_CENTER, TA_LEFT
from reportlab.lib.units import inch
from xml.sax.saxutils import escape

# --- 1. CONFIGURATION ---
PAGE_TITLE = "Astra Resume Engine"
ASTRA_PROMPT = """
Role: You are Astra, a Senior Technical Recruiter.
Objective: Rewrite the resume to match the JD with 98% alignment.

CRITICAL OUTPUT RULES:
1. OUTPUT JSON ONLY.
2. KEYS: 'candidate_name', 'candidate_title', 'contact_info', 'summary', 'skills', 'experience', 'education', 'target_company'.
3. SKILLS: Dense 6-7 categories. Values must be comma-separated strings.
4. EXPERIENCE: Must include 'role_title', 'company', 'dates', 'location', 'responsibilities' (list), 'achievements' (list).
5. EDUCATION: Return a LIST of objects: [{"degree": "...", "college": "..."}].
6. TARGET COMPANY: Extract the company name from the JD text. If not found, return "Company".
"""

# --- 2. DATA NORMALIZER ---
def clean_skill_string(skill_str):
    if not isinstance(skill_str, str): return str(skill_str)
    if skill_str.strip().startswith("["):
        try:
            list_match = re.search(r"\[(.*?)\]", skill_str)
            if list_match:
                actual_list = ast.literal_eval(list_match.group(0))
                extra_part = skill_str[list_match.end():].strip().lstrip(",").strip()
                clean_str = ", ".join([str(s) for s in actual_list])
                if extra_part: clean_str += f", {extra_part}"
                return clean_str
        except: pass
    return skill_str

def normalize_schema(data):
    if not isinstance(data, dict): return {"summary": str(data), "skills": {}, "experience": []}

    normalized = {}

    # 1. Contact/Name
    contact_src = data.get("Contact", data.get("contact_info", {}))
    if isinstance(contact_src, dict):
        normalized['candidate_name'] = contact_src.get("Name", data.get('candidate_name', ''))
        normalized['candidate_title'] = contact_src.get("Title", data.get('candidate_title', ''))
        parts = []
        for k in ["Phone", "phone", "Email", "email", "Location", "location"]:
            val = contact_src.get(k)
            if val and val not in parts: parts.append(str(val))
        normalized['contact_info'] = " | ".join(parts)
    else:
        normalized['candidate_name'] = data.get('candidate_name', data.get('Name', ''))
        normalized['candidate_title'] = data.get('candidate_title', data.get('Title', ''))
        normalized['contact_info'] = str(data.get('contact_info', data.get('Contact', '')))

    # 2. Summary
    normalized['summary'] = data.get('summary', data.get('Professional_Profile', data.get('Profile', '')))

    # 3. Skills
    raw_skills = data.get('skills', data.get('Skills_Technologies', {}))
    normalized['skills'] = {}
    if isinstance(raw_skills, dict):
        for k, v in raw_skills.items():
            normalized['skills'][k] = clean_skill_string(str(v))
    elif isinstance(raw_skills, list):
        normalized['skills'] = {"General Skills": ", ".join([str(s) for s in raw_skills])}

    # 4. Experience
    raw_exp = data.get('experience', data.get('Professional_Experience', []))
    norm_exp = []
    if isinstance(raw_exp, list):
        for role in raw_exp:
            new_role = {}
            if isinstance(role, dict):
                new_role['role_title'] = role.get('role_title', role.get('Title', ''))
                new_role['company'] = role.get('company', role.get('Company', ''))
                new_role['dates'] = role.get('dates', role.get('Dates', ''))
                new_role['location'] = role.get('location', role.get('Location', ''))
                new_role['responsibilities'] = role.get('responsibilities', role.get('Responsibilities', []))
                new_role['achievements'] = role.get('achievements', role.get('Achievements', []))
            norm_exp.append(new_role)
    normalized['experience'] = norm_exp

    # 5. Education
    raw_edu = data.get('education', data.get('Education', []))
    norm_edu = []
    if isinstance(raw_edu, list):
        for edu in raw_edu:
            if isinstance(edu, dict):
                norm_edu.append({
                    'degree': edu.get('degree', edu.get('Degree', '')),
                    'college': edu.get('college', edu.get('Institution', ''))
                })
            elif isinstance(edu, str):
                 norm_edu.append({'degree': edu, 'college': ''})
    elif isinstance(raw_edu, dict):
        for k, v in raw_edu.items():
            norm_edu.append({'degree': k, 'college': str(v)})
    elif isinstance(raw_edu, str):
        norm_edu.append({'degree': raw_edu, 'college': ''})
    normalized['education'] = norm_edu

    # 6. Target Company
    normalized['target_company'] = data.get('target_company', 'Company')

    return normalized

# --- 3. JUDGE & UTILS ---
def calculate_groq_score(resume_json, jd_text, groq_api_key):
    if not groq_api_key: return {"score": 0, "reasoning": "No Groq Key"}
    client = Groq(api_key=groq_api_key)
    try:
        prompt = f"""
        You are an ATS. Compare this JSON Resume vs JD.
        Output STRICT JSON: {{'score': int, 'reasoning': '1 short sentence'}}
        SCORE 0-100.
        RESUME: {str(resume_json)[:2500]}
        JD: {jd_text[:2500]}
        """
        completion = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}
        )
        return json.loads(completion.choices[0].message.content)
    except:
        return {"score": 0, "reasoning": "Groq Error"}

def repair_json(text):
    text = text.strip()
    if "```" in text:
        match = re.search(r"```(?:json)?(.*?)```", text, re.DOTALL)
        if match: return json.loads(match.group(1))
    try:
        start = text.find("{")
        end = text.rfind("}") + 1
        return json.loads(text[start:end])
    except:
        return None

def expand_skills_dense(skills):
    if not skills: return {}
    EXPANSIONS = {"Pandas": "Polars, Dask", "AWS": "EC2, S3, Lambda", "Python": "FastAPI, Flask", "SQL": "Postgres, NoSQL", "K8s": "Helm, ArgoCD"}
    for cat, tools in skills.items():
        tools_str = str(tools)
        for k, v in EXPANSIONS.items():
            if k in tools_str and v not in tools_str: tools_str += f", {v}"
        skills[cat] = tools_str
    return skills

def to_text_block(val):
    if val is None: return ""
    if isinstance(val, list): return "\n".join([str(x) for x in val])
    return str(val)

# --- 4. GENERATION ---
def analyze_and_generate(google_key, groq_key, resume_text, jd_text):
    client = genai.Client(api_key=google_key)
    try:
        response = client.models.generate_content(
            model="gemini-flash-latest",
            contents=f"{ASTRA_PROMPT}\n\nRESUME:\n{resume_text}\n\nJD:\n{jd_text}",
            config=types.GenerateContentConfig(response_mime_type="application/json")
        )

        raw_data = repair_json(response.text)
        if not raw_data: return {"error": "Parsing Failed", "raw": response.text}

        data = normalize_schema(raw_data)
        data['skills'] = expand_skills_dense(data.get('skills', {}))

        judge = calculate_groq_score(data, jd_text, groq_key)
        data['ats_score'] = judge.get('score', 0)
        data['ats_reason'] = judge.get('reasoning', '')

        data['raw_debug'] = raw_data
        return data
    except Exception as e:
        return {"error": str(e)}

# --- 5. DOCX RENDERER ---
def set_font(run, size, bold=False):
    run.font.name = 'Times New Roman'
    run.font.size = Pt(size)
    run.bold = bold
    try: run._element.rPr.rFonts.set(qn('w:eastAsia'), 'Times New Roman')
    except: pass

def create_doc(data):
    doc = Document()
    s = doc.sections[0]
    s.left_margin = s.right_margin = s.top_margin = s.bottom_margin = Inches(0.5)

    # Header
    header_data = [
        (data.get('candidate_name', ''), 28, True),
        (data.get('candidate_title', ''), 14, True),
        (data.get('contact_info', ''), 12, True)
    ]
    for txt, sz, b in header_data:
        p = doc.add_paragraph()
        p.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
        p.paragraph_format.space_before = Pt(0)
        p.paragraph_format.space_after = Pt(0)
        run = p.add_run(to_text_block(txt))
        if sz == 28: run.font.all_caps = True
        set_font(run, sz, b)

    def add_sec(title):
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(12)
        p.paragraph_format.space_after = Pt(2)
        set_font(p.add_run(title), 12, True)

    def add_body(txt, bullet=False):
        style = 'List Bullet' if bullet else 'Normal'
        p = doc.add_paragraph(style=style)
        p.alignment = WD_PARAGRAPH_ALIGNMENT.JUSTIFY
        p.paragraph_format.space_after = Pt(0)
        set_font(p.add_run(to_text_block(txt)), 12)

    add_sec("Professional Profile")
    add_body(data.get('summary', ''))

    add_sec("Key Skills/ Tools & Technologies")
    for k, v in data.get('skills', {}).items():
        p = doc.add_paragraph(style='List Bullet')
        p.alignment = WD_PARAGRAPH_ALIGNMENT.JUSTIFY
        p.paragraph_format.space_after = Pt(0)
        set_font(p.add_run(f"{k}: "), 12, True)
        set_font(p.add_run(to_text_block(v)), 12)

    add_sec("Professional Experience")
    for role in data.get('experience', []):
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(6)
        p.paragraph_format.space_after = Pt(0)
        line = f"{role.get('role_title')} | {role.get('company')} | {role.get('location')} | {role.get('dates')}"
        set_font(p.add_run(to_text_block(line)), 12, True)

        resps = role.get('responsibilities', [])
        if isinstance(resps, str): resps = resps.split('\n')
        for r in resps: add_body(r, bullet=True)

        achs = role.get('achievements', [])
        if isinstance(achs, str): achs = achs.split('\n')
        if achs:
            p = doc.add_paragraph()
            p.indent_level = 1
            p.paragraph_format.space_before = Pt(2)
            set_font(p.add_run("Achievements:"), 12, True)
            for a in achs: add_body(a, bullet=True)

    add_sec("Education")
    for edu in data.get('education', []):
        text = f"{edu.get('degree', '')}, {edu.get('college', '')}"
        add_body(text, bullet=True)

    return doc

# --- 6. PDF RENDERER (FIXED) ---
def create_pdf(data):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter,
                            leftMargin=0.5*inch, rightMargin=0.5*inch,
                            topMargin=0.5*inch, bottomMargin=0.5*inch)
    styles = getSampleStyleSheet()
    style_normal = ParagraphStyle('AstraNormal', parent=styles['Normal'], fontName='Times-Roman', fontSize=12, leading=14, alignment=TA_JUSTIFY, spaceAfter=0)
    style_header_name = ParagraphStyle('AstraHeaderName', parent=styles['Normal'], fontName='Times-Bold', fontSize=28, leading=30, alignment=TA_CENTER, spaceAfter=0)
    style_header_title = ParagraphStyle('AstraHeaderTitle', parent=styles['Normal'], fontName='Times-Bold', fontSize=14, leading=16, alignment=TA_CENTER, spaceAfter=0)
    style_header_contact = ParagraphStyle('AstraHeaderContact', parent=styles['Normal'], fontName='Times-Bold', fontSize=12, leading=14, alignment=TA_CENTER, spaceAfter=6)
    style_section = ParagraphStyle('AstraSection', parent=styles['Normal'], fontName='Times-Bold', fontSize=12, leading=14, alignment=TA_LEFT, spaceBefore=12, spaceAfter=2)

    def clean(txt):
        if txt is None: return ""
        txt = to_text_block(txt)
        return escape(txt).replace('\n', '<br/>')

    elements = []
    elements.append(Paragraph(clean(data.get('candidate_name', '')), style_header_name))
    elements.append(Paragraph(clean(data.get('candidate_title', '')), style_header_title))
    elements.append(Paragraph(clean(data.get('contact_info', '')), style_header_contact))

    elements.append(Paragraph("Professional Profile", style_section))
    elements.append(Paragraph(clean(data.get('summary', '')), style_normal))

    elements.append(Paragraph("Key Skills/ Tools & Technologies", style_section))
    skill_items = []
    for k, v in data.get('skills', {}).items():
        text = f"<b>{clean(k)}:</b> {clean(v)}"
        skill_items.append(ListItem(Paragraph(text, style_normal), leftIndent=0))
    if skill_items: elements.append(ListFlowable(skill_items, bulletType='bullet', start='•', leftIndent=15))

    elements.append(Paragraph("Professional Experience", style_section))
    for role in data.get('experience', []):
        line = f"{role.get('role_title')} | {role.get('company')} | {role.get('location')} | {role.get('dates')}"
        elements.append(Paragraph(f"<b>{clean(line)}</b>", style_normal))
        elements.append(Spacer(1, 2))

        role_bullets = []
        resps = role.get('responsibilities', [])
        if isinstance(resps, str): resps = resps.split('\n')
        for r in resps:
            if r.strip(): role_bullets.append(ListItem(Paragraph(clean(r), style_normal), leftIndent=0))
        if role_bullets: elements.append(ListFlowable(role_bullets, bulletType='bullet', start='•', leftIndent=15))

        achs = role.get('achievements', [])
        if isinstance(achs, str): achs = achs.split('\n')
        if achs:
            elements.append(Paragraph("<b>Achievements:</b>", style_normal))
            ach_bullets = []
            for a in achs:
                if a.strip(): ach_bullets.append(ListItem(Paragraph(clean(a), style_normal), leftIndent=0))
            if ach_bullets: elements.append(ListFlowable(ach_bullets, bulletType='bullet', start='•', leftIndent=25))
        elements.append(Spacer(1, 6))

    elements.append(Paragraph("Education", style_section))
    edu_bullets = []
    for edu in data.get('education', []):
        text = f"{edu.get('degree', '')}, {edu.get('college', '')}"
        edu_bullets.append(ListItem(Paragraph(clean(text), style_normal), leftIndent=0))
    if edu_bullets: elements.append(ListFlowable(edu_bullets, bulletType='bullet', start='•', leftIndent=15))

    doc.build(elements)
    buffer.seek(0)
    return buffer.getvalue()

# --- 7. UI LAYER ---
st.set_page_config(page_title=PAGE_TITLE, layout="wide")
if 'data' not in st.session_state: st.session_state['data'] = None
# MEMORY FOR REDO
if 'saved_base' not in st.session_state: st.session_state['saved_base'] = ""
if 'saved_jd' not in st.session_state: st.session_state['saved_jd'] = ""

with st.sidebar:
    st.header("🔑 API Keys")
    google_key = st.text_input("Google API Key", type="password")
    groq_key = st.text_input("Groq API Key", type="password")

    st.divider()
    if st.button("🗑️ Full Reset (Clear Everything)"):
        st.session_state['data'] = None
        st.session_state['saved_base'] = ""
        st.session_state['saved_jd'] = ""
        st.rerun()

if not st.session_state['data']:
    c1, c2 = st.columns(2)
    # The 'value' is bound to session state, so it persists unless explicitly cleared
    base = c1.text_area("Base Resume", st.session_state['saved_base'], height=300)
    jd = c2.text_area("Job Description", st.session_state['saved_jd'], height=300)

    if st.button("Generate Resume"):
        if google_key and groq_key and base and jd:
            # SAVE INPUTS
            st.session_state['saved_base'] = base
            st.session_state['saved_jd'] = jd

            with st.spinner("Astra Architecting..."):
                data = analyze_and_generate(google_key, groq_key, base, jd)
                if "error" in data: st.error(data['error'])
                else:
                    st.session_state['data'] = data
                    st.rerun()
else:
    data = st.session_state['data']
    st.metric("Groq ATS Score", f"{data.get('ats_score', 0)}%")
    st.info(data.get('ats_reason', ''))

    with st.form("edit_form"):
        c1, c2, c3 = st.columns(3)
        data['candidate_name'] = c1.text_input("Name", to_text_block(data.get('candidate_name')), key="name")
        data['candidate_title'] = c2.text_input("Title", to_text_block(data.get('candidate_title')), key="title")
        data['contact_info'] = c3.text_input("Contact", to_text_block(data.get('contact_info')), key="contact")

        data['summary'] = st.text_area("Summary", to_text_block(data.get('summary')), height=100, key="summary")

        st.subheader("Skills")
        skills = data.get('skills', {})
        new_skills = {}
        for i, (k, v) in enumerate(skills.items()):
            new_val = st.text_area(k, to_text_block(v), key=f"skill_{i}", height=70)
            new_skills[k] = new_val.replace('\n', ', ')
        data['skills'] = new_skills

        st.subheader("Experience")
        for i, role in enumerate(data.get('experience', [])):
            with st.expander(f"Role {i+1}: {role.get('company', 'Company')}"):
                c1, c2 = st.columns(2)
                role['role_title'] = c1.text_input("Title", to_text_block(role.get('role_title')), key=f"job_title_{i}")
                role['company'] = c2.text_input("Company", to_text_block(role.get('company')), key=f"job_comp_{i}")
                c3, c4 = st.columns(2)
                role['dates'] = c3.text_input("Dates", to_text_block(role.get('dates')), key=f"job_dates_{i}")
                role['location'] = c4.text_input("Location", to_text_block(role.get('location')), key=f"job_loc_{i}")
                resps = to_text_block(role.get('responsibilities'))
                role['responsibilities'] = st.text_area("Responsibilities", resps, height=150, key=f"resp_{i}")
                achs = to_text_block(role.get('achievements'))
                role['achievements'] = st.text_area("Achievements", achs, height=100, key=f"ach_{i}")

        st.subheader("Education")
        education = data.get('education', [])
        for i, edu in enumerate(education):
            with st.expander(f"Education {i+1}"):
                c1, c2 = st.columns(2)
                edu['degree'] = c1.text_input("Degree", to_text_block(edu.get('degree')), key=f"edu_deg_{i}")
                edu['college'] = c2.text_input("College", to_text_block(edu.get('college')), key=f"edu_col_{i}")

        if st.form_submit_button("Save Changes"):
            st.session_state['data'] = data
            st.success("Saved!")
            st.rerun()

    st.subheader("Export Resume")
    c_name = data.get('candidate_name', 'Candidate')
    default_company = data.get('target_company', 'Company')
    target_company = st.text_input("Target Company (for filename):", default_company)
    safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', c_name.strip().replace(' ', '_'))
    safe_company = re.sub(r'[^a-zA-Z0-9_-]', '_', target_company.strip())
    final_filename = f"{safe_name}_{safe_company}"

    c1, c2 = st.columns(2)
    doc = create_doc(data)
    bio = io.BytesIO()
    doc.save(bio)
    c1.download_button(label=f"📄 Download DOCX", data=bio.getvalue(), file_name=f"{final_filename}.docx", mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document", type="primary")

    try:
        pdf_data = create_pdf(data)
        c2.download_button(label=f"📕 Download PDF", data=pdf_data, file_name=f"{final_filename}.pdf", mime="application/pdf", type="secondary")
    except Exception as e: c2.error(f"PDF Error: {e}")

    # --- ACTION BUTTONS (SMART RESET) ---
    st.divider()
    c3, c4 = st.columns(2)

    if c3.button("♻️ Redo / Improve"):
        if st.session_state['saved_base'] and st.session_state['saved_jd']:
            with st.spinner("Re-Architecting..."):
                data = analyze_and_generate(google_key, groq_key, st.session_state['saved_base'], st.session_state['saved_jd'])
                if "error" in data: st.error(data['error'])
                else:
                    st.session_state['data'] = data
                    st.rerun()

    if c4.button("Start New Application (Keeps Resume)"):
        # Clear generated data and JD, but KEEP saved_base
        st.session_state['data'] = None
        st.session_state['saved_jd'] = ""
        st.rerun()
