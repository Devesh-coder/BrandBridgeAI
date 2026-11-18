"""Demo data for ABC Technologies (a fictional company).

    flask --app app seed-demo

Creates the organisation, three demo users, a full brand guideline with approved facts and an approved
logo, and writes sample images to ./sample_data for testing the visual checker.
Demo passwords are for local demonstrations ONLY – never use this seed in production.
"""
import os
import shutil
import uuid

from flask import current_app
from PIL import Image, ImageDraw, ImageFont

from extensions import db
from models import ApprovedFact, BrandAsset, BrandGuideline, Organisation, User

DEMO_USERS = [
    ("Asha Admin", "admin@abc.demo", "Admin@123", "admin"),
    ("Chirag Creator", "creator@abc.demo", "Creator@123", "creator"),
    ("Meera Manager", "reviewer@abc.demo", "Reviewer@123", "reviewer"),
]
DEMO_CONTENT = ("ABC Tech offers the world's best software with 100% guaranteed results. "
                "Buy now and become successful instantly.")
SAMPLE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample_data")


def _font(size):
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "DejaVuSans-Bold.ttf", "arialbd.ttf", "Arial Bold.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def make_logo(path, fill=(37, 99, 235)):
    im = Image.new("RGB", (320, 320), "white")
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((20, 20, 300, 300), radius=48, fill=fill)
    d.text((160, 160), "ABC", font=_font(96), fill="white", anchor="mm")
    d.rectangle((70, 235, 250, 245), fill=(245, 158, 11))
    im.save(path)


def make_samples():
    """Write demo images into ./sample_data."""
    os.makedirs(SAMPLE_DIR, exist_ok=True)
    make_logo(os.path.join(SAMPLE_DIR, "abc_logo_approved.png"))
    # on-brand banner: approved logo on navy background
    ban = Image.new("RGB", (900, 300), (15, 23, 42))
    logo = Image.open(os.path.join(SAMPLE_DIR, "abc_logo_approved.png")).resize((200, 200))
    ban.paste(logo, (40, 50))
    ImageDraw.Draw(ban).text((300, 150), "ABC Technologies", font=_font(48), fill="white", anchor="lm")
    ban.save(os.path.join(SAMPLE_DIR, "banner_on_brand.png"))
    # off-brand banner: wrong colours and a recoloured, distorted logo
    off = Image.new("RGB", (900, 300), (220, 38, 38))
    d = ImageDraw.Draw(off)
    d.rectangle((450, 0, 900, 300), fill=(34, 197, 94))
    d.ellipse((60, 70, 220, 230), fill=(250, 204, 21))
    d.text((140, 150), "ABC", font=_font(40), fill=(120, 20, 20), anchor="mm")
    d.text((680, 150), "MEGA SALE", font=_font(52), fill="white", anchor="mm")
    off.save(os.path.join(SAMPLE_DIR, "banner_off_brand.png"))
    with open(os.path.join(SAMPLE_DIR, "demo_content.txt"), "w") as fh:
        fh.write(DEMO_CONTENT + "\n")


def seed_demo():
    cfg = current_app.config
    if Organisation.query.filter_by(name="ABC Technologies").first():
        print("Demo data already exists – nothing to do.")
        return
    if cfg.get("APP_ENV") == "production":
        print("Refusing to seed demo accounts in production.")
        return
    make_samples()
    org = Organisation(name="ABC Technologies", invite_code="ABC-DEMO")
    db.session.add(org)
    db.session.flush()
    users = {}
    for name, email, pw, role in DEMO_USERS:
        u = User(org_id=org.id, name=name, email=email, role=role)
        u.set_password(pw)
        db.session.add(u)
        users[role] = u
    db.session.flush()

    g = BrandGuideline(
        org_id=org.id, created_by=users["admin"].id, title="ABC Technologies Brand Guidelines 2026",
        company_name="ABC Technologies", tone_style="friendly",
        description="ABC Technologies builds business software and cloud tools for small and mid-sized companies in India.",
        voice_tone="Professional and friendly. Clear, helpful and confident without exaggeration. Avoid slang and hype.",
        approved_terms=("ABC Technologies | ABC Tech, ABC Tech Ltd, ABC Technology\n"
                        "ABC CloudSuite | ABC Cloud Suite, ABC Cloudsuite"),
        prohibited_words="cheap => affordable\nguru => expert\nrockstar => skilled professional\nninja => specialist",
        unsupported_claims=("100% guaranteed results => a focus on quality results\n"
                            "guaranteed results => a focus on quality results\ninstant success => steady progress\n"
                            "risk-free => low-commitment"),
        brand_colours="#2563EB, #0F172A, #FFFFFF, #F8FAFC, #F59E0B",
        slogans="Technology That Moves You Forward", is_active=True)
    db.session.add(g)
    db.session.flush()
    for stmt, src in [
        ("ABC Technologies was founded in 2015.", "Company profile 2026 (about.abc-demo.example)"),
        ("ABC Technologies serves over 500 business customers.", "Annual report FY2025-26"),
        ("ABC Technologies has offices in Pune and Bengaluru.", "Company profile 2026"),
        ("ABC CloudSuite is certified to ISO 27001.", "Certificate ISO-27001-2025-ABC"),
        ("Customer support is available Monday to Saturday, 9 AM to 6 PM IST.", "Support policy v3"),
    ]:
        g.facts.append(ApprovedFact(statement=stmt, source=src))

    os.makedirs(cfg["UPLOAD_FOLDER"], exist_ok=True)
    stored = f"{uuid.uuid4().hex}.png"
    shutil.copy(os.path.join(SAMPLE_DIR, "abc_logo_approved.png"), os.path.join(cfg["UPLOAD_FOLDER"], stored))
    g.assets.append(BrandAsset(org_id=org.id, kind="logo", original_filename="abc_logo_approved.png",
                               stored_filename=stored, mime_type="image/png",
                               size_bytes=os.path.getsize(os.path.join(SAMPLE_DIR, "abc_logo_approved.png")),
                               uploaded_by=users["admin"].id))
    db.session.commit()
    print("Demo data created for ABC Technologies.")
    print("  Invite code : ABC-DEMO")
    for name, email, pw, role in DEMO_USERS:
        print(f"  {role:9s}: {email} / {pw}")
    print(f"  Sample images written to {SAMPLE_DIR}")
