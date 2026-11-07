"""Render the sample credit applications as scanned, hand-filled forms.

The field values come from backend/vouch/fixtures/samples.json, which is also the
answer key the tests compare extraction against. Output is an image-only PDF
(no text layer), the way a real scanner produces it.

    cd backend && .venv/bin/python ../samples/generate_samples.py
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
FIXTURE = REPO / "backend" / "vouch" / "fixtures" / "samples.json"
HAND = HERE / "fonts" / "Caveat-Medium.ttf"

SANS_CANDIDATES = [
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
    "C:/Windows/Fonts/arial.ttf",
]
BOLD_CANDIDATES = [
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]

W, H = 1275, 1650  # US Letter at 150 dpi
INK = (28, 46, 120)
PRINT = (25, 25, 25)
RULE = (120, 120, 120)


def font(candidates: list[str], size: int) -> ImageFont.FreeTypeFont:
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


def money(v):
    return f"${v:,.0f}" if v else ""


class Form:
    def __init__(self, seed: int) -> None:
        self.img = Image.new("RGB", (W, H), (255, 255, 255))
        self.d = ImageDraw.Draw(self.img)
        self.rng = random.Random(seed)
        self.label = font(SANS_CANDIDATES, 17)
        self.small = font(SANS_CANDIDATES, 15)
        self.head = font(BOLD_CANDIDATES, 21)
        self.title = font(BOLD_CANDIDATES, 34)
        self.hand = ImageFont.truetype(str(HAND), 36)
        self.hand_small = ImageFont.truetype(str(HAND), 31)

    def section(self, y: int, text: str) -> int:
        self.d.rectangle([60, y, W - 60, y + 32], fill=(232, 232, 232))
        self.d.text((72, y + 5), text.upper(), font=self.head, fill=PRINT)
        return y + 46

    def field(self, x: int, y: int, w: int, label: str, value: str, small: bool = False) -> None:
        self.d.text((x, y), label, font=self.label, fill=PRINT)
        lw = self.d.textlength(label, font=self.label) + 10
        self.d.line([x + lw, y + 22, x + w, y + 22], fill=RULE, width=1)
        if value:
            jitter = self.rng.randint(-3, 2)
            f = self.hand_small if small or self.d.textlength(value, font=self.hand) > (w - lw - 10) else self.hand
            self.d.text((x + lw + 6, y - 14 + jitter), value, font=f, fill=INK)

    def render(self, seller: str, app: dict) -> Image.Image:
        d = self.d
        a = app["applicant"]
        d.text((60, 52), seller.upper(), font=self.head, fill=PRINT)
        d.text((60, 84), "Commercial Credit Application", font=self.title, fill=PRINT)
        d.text((W - 360, 62), "Credit Dept. Form CR-2", font=self.small, fill=RULE)
        d.text((W - 360, 84), "Return to credit@northlinesupply", font=self.small, fill=RULE)

        y = self.section(140, "Business information")
        addr = a["address"]
        self.field(60, y, 760, "Legal business name", a["legal_name"])
        self.field(840, y, 375, "DBA", a.get("dba") or "")
        y += 52
        self.field(60, y, 360, "Entity type", a.get("entity_type") or "")
        self.field(440, y, 300, "State of inc.", a.get("state_of_incorporation") or "")
        self.field(760, y, 455, "Federal EIN", a.get("ein") or "")
        y += 52
        self.field(60, y, 1155, "Street address", f"{addr['street']}, {addr['city']}, {addr['state']} {addr['postal_code']}")
        y += 52
        self.field(60, y, 400, "Phone", a.get("phone") or "")
        self.field(480, y, 735, "Email", a.get("email") or "")
        y += 52
        self.field(60, y, 460, "Website", a.get("website") or "")
        self.field(540, y, 300, "Years in business", str(int(a["years_in_business"])) if a.get("years_in_business") else "")
        self.field(860, y, 355, "Annual revenue", money(a.get("annual_revenue")))
        y += 52
        self.field(60, y, 640, "Principal / owner", a.get("principal_name") or "")
        self.field(720, y, 495, "Title", a.get("principal_title") or "")

        y = self.section(y + 62, "Credit requested")
        self.field(60, y, 520, "Credit limit requested", money(app.get("requested_limit")))
        self.field(600, y, 615, "Terms requested", app.get("requested_terms") or "")

        b = app.get("bank") or {}
        y = self.section(y + 62, "Bank reference")
        self.field(60, y, 640, "Bank", b.get("bank_name", ""))
        self.field(720, y, 495, "Contact", b.get("contact_name") or "")
        y += 52
        self.field(60, y, 400, "Phone", b.get("phone") or "")
        self.field(480, y, 480, "Email", b.get("email") or "", small=True)
        self.field(980, y, 235, "Acct (last 4)", b.get("account_last4") or "")

        y = self.section(y + 62, "Trade references (suppliers who extend you credit)")
        for i, r in enumerate(app["trade_references"], start=1):
            ra = r["address"]
            d.text((60, y + 4), f"{i}.", font=self.head, fill=PRINT)
            self.field(95, y, 620, "Company", r["company"])
            self.field(735, y, 480, "Contact", r.get("contact_name") or "")
            y += 50
            self.field(95, y, 360, "Phone", r.get("phone") or "")
            self.field(475, y, 740, "Email", r.get("email") or "", small=True)
            y += 50
            self.field(95, y, 1120, "Address", f"{ra['street']}, {ra['city']}, {ra['state']} {ra['postal_code']}", small=True)
            y += 62

        y = self.section(y + 4, "Authorization")
        text = (
            f"The undersigned certifies that the information above is true and authorizes {seller} to contact the bank "
            "and trade references listed, and to obtain credit reports, for the purpose of evaluating this application."
        )
        words, line, lines = text.split(), "", []
        for w in words:
            trial = (line + " " + w).strip()
            if d.textlength(trial, font=self.label) > W - 140:
                lines.append(line)
                line = w
            else:
                line = trial
        lines.append(line)
        for ln in lines:
            d.text((60, y), ln, font=self.label, fill=PRINT)
            y += 24
        y += 34
        sig = app["signature"]
        self.field(60, y, 520, "Signature", "")
        if sig.get("signed"):
            sig_font = ImageFont.truetype(str(HAND), 48)
            d.text((180, y - 30), sig.get("signer_name") or "", font=sig_font, fill=INK)
        self.field(600, y, 380, "Printed name", sig.get("signer_name") or "")
        self.field(1000, y, 215, "Date", sig.get("date") or "")
        y += 52
        self.field(60, y, 520, "Title", sig.get("signer_title") or "")
        return self.img


def scan(img: Image.Image, seed: int) -> Image.Image:
    rng = random.Random(seed)
    img = img.rotate(rng.uniform(-0.6, 0.6), resample=Image.BICUBIC, expand=False, fillcolor=(255, 255, 255))
    img = img.filter(ImageFilter.GaussianBlur(0.55)).convert("L")
    noise = Image.effect_noise(img.size, 9).convert("L")
    img = Image.blend(img, noise, 0.06)
    img = img.point(lambda p: min(255, int(p * 0.96 + 6)))
    return img.convert("RGB")


def main() -> None:
    data = json.loads(FIXTURE.read_text())
    for i, sample in enumerate(data["samples"]):
        page = Form(seed=11 + i).render(data["seller"], sample["application"])
        out = REPO / sample["file"]
        out.parent.mkdir(parents=True, exist_ok=True)
        scan(page, seed=7 + i).save(out, "PDF", resolution=150)
        print(f"wrote {out.relative_to(REPO)}")


if __name__ == "__main__":
    main()
