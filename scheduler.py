import anthropic
import os
import json
import smtplib
import requests
import urllib.parse
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timedelta

# City rotation order
CITIES = [
    "Los Angeles, CA",
    "Orange County, CA",
    "San Diego, CA",
    "Riverside, CA",
    "San Bernardino, CA",
    "Ventura, CA",
    "Sacramento, CA",
    "San Francisco, CA",
    "Fresno, CA",
    "Bakersfield, CA",
    "Las Vegas, NV",
    "Phoenix, AZ",
    "Portland, OR",
    "Seattle, WA",
    "Denver, CO",
    "Dallas, TX",
    "Houston, TX",
    "Miami, FL",
    "Atlanta, GA",
    "Chicago, IL",
    "New York, NY",
]

CITY_THRESHOLD = 40
SHEET_URL = os.environ.get("SHEET_URL", "")

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

def get_sheet_data():
    """Get all contacted orgs from Google Sheet via Apps Script"""
    try:
        res = requests.get(f"{SHEET_URL}?action=getall", timeout=15)
        data = res.json()
        return data.get("records", [])
    except Exception as e:
        print(f"Error getting sheet data: {e}")
        return []

def get_current_city(records):
    city_counts = {}
    for row in records:
        city = row.get("City", "")
        if city:
            city_counts[city] = city_counts.get(city, 0) + 1

    for city in CITIES:
        city_short = city.split(",")[0].strip()
        count = sum(v for k, v in city_counts.items() if city_short.lower() in k.lower())
        if count < CITY_THRESHOLD:
            return city
    return CITIES[-1]

def find_phone(org):
    try:
        prompt = f"""What is the main phone number for {org['name']} in {org['city']}?
{f"Their website is {org['website']}." if org.get('website') else ''}
Return ONLY a JSON object: {{"phone": "..."}}
If unknown: {{"phone": "Not found"}}"""
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}]
        )
        text = response.content[0].text.strip()
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()
        parsed = json.loads(text)
        return parsed.get("phone", "Not found")
    except:
        return "Not found"

def log_to_sheet(org, status="Contacted"):
    """Log a new org to Google Sheet via Apps Script"""
    try:
        requests.post(SHEET_URL, json={
            "name": org["name"],
            "type": org["type"],
            "city": org["city"],
            "contact": org.get("contact", ""),
            "website": org.get("website", ""),
            "status": status
        }, timeout=15)
    except Exception as e:
        print(f"Error logging to sheet: {e}")

def search_orgs(location, exclude_names):
    city = location.split(",")[0].strip()
    state = location.split(",")[1].strip() if "," in location else "CA"
    exclude_str = ""
    if exclude_names:
        exclude_str = "\n\nDo NOT include these already-contacted organizations:\n" + "\n".join(f"- {n}" for n in exclude_names[:50])

    prompt = f"""Find exactly 5 veterans groups, exactly 5 first responder groups (fire/police/EMS), and exactly 10 tactical training companies in or near {city}, {state}.

Include:
- Veterans groups: VFW posts, American Legion posts, veteran service organizations
- First responder groups: firefighter locals/unions, police protective leagues, EMS associations
- Tactical training companies: veteran-owned firearms training, self-defense schools, shooting ranges
{exclude_str}

For each organization:
- Real name
- City
- Contact email (only if confident it's real — say "No email found" if unsure)
- Website if known
- Type: exactly one of: "Fire department", "Police & law enforcement", "EMS & paramedics", "Veterans group", "Tactical training group"

Return ONLY a valid JSON array:
[{{"name":"...","city":"{city}","contact":"...","website":"...","type":"..."}}]"""

    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}]
    )
    text = response.content[0].text.strip()
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()
    start = text.find("[")
    end = text.rfind("]") + 1
    if start >= 0 and end > start:
        text = text[start:end]
    orgs = json.loads(text)
    exclude_lower = [e.lower() for e in exclude_names]
    return [o for o in orgs if o.get("name", "").lower() not in exclude_lower][:20]

def generate_email(org):
    prompt = f"""Write a warm outreach email from Michael D'Angelo, founder of Rapid Fire Comedy Tour.

Use this template but personalize for the specific org:

Hi there,

My name's Michael D'Angelo — I'm a Marine Corps machine gunner turned comedian, which is probably the only career pivot that makes less sense than it sounds.

I started the Rapid Fire Comedy Tour to bring live stand-up comedy to the people who spend their days running toward danger. First responders, military, and training communities — the folks who deserve a good laugh more than anyone.

I'd love to bring a show to [ORG NAME]. I have a roster of comedians I book for events across the country, and there's something special about making a room full of [THEIR COMMUNITY] laugh. Beyond stand-up, I'm also available as a guest speaker or emcee for events, banquets, graduations, or any gathering where you want someone who actually gets your world.

If you've got an upcoming event, holiday party, awards banquet, or even just a slow Tuesday — I'd love to have a conversation about making it memorable. You can also visit our website at www.rapidfirecomedytour.org to read letters of recommendation from previous shows.

Worth a chat?

Semper Fi,
Michael D'Angelo
Founder, Rapid Fire Comedy Tour
info@rapidfirecomedytour.org
www.rapidfirecomedytour.org

Rules: No word "free". Under 200 words. Personal and warm.

Organization: {org['name']}
Type: {org['type']}
City: {org['city']}

Return JSON only: {{"subject": "...", "body": "..."}}"""

    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )
    text = response.content[0].text.strip()
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()
    return json.loads(text)

def send_digest_email(orgs_and_emails, call_list, current_city):
    gmail_user = os.environ.get("GMAIL_USER")
    gmail_password = os.environ.get("GMAIL_APP_PASSWORD")

    if not gmail_user or not gmail_password:
        print("Gmail credentials not set — skipping digest email")
        return

    html = f"""
    <html><body style="font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px;">
    <h1 style="color: #ff4d00;">🔥 Rapid Fire Daily Outreach Digest</h1>
    <p style="color: #666;">Date: {datetime.now().strftime("%B %d, %Y")} | City: {current_city}</p>
    <p>📧 <strong>{len(orgs_and_emails)} emails ready to send</strong> | 📞 <strong>{len(call_list)} orgs to call</strong></p>
    <hr/>
    <h2>📧 Emails — click to open in Gmail and send</h2>
    """

    for i, (org, email) in enumerate(orgs_and_emails):
        contact = org.get("contact", "No email found")
        has_email = contact and contact != "No email found"
        gmail_link = ""
        if has_email:
            subject = urllib.parse.quote(email["subject"])
            body = urllib.parse.quote(email["body"])
            gmail_link = f'<a href="https://mail.google.com/mail/?view=cm&fs=1&to={contact}&su={subject}&body={body}&from=info@rapidfirecomedytour.org" style="background:#ff4d00;color:white;padding:8px 16px;border-radius:6px;text-decoration:none;font-weight:bold;">Open in Gmail ↗</a>'

        html += f"""
        <div style="border:1px solid #ddd;border-radius:8px;padding:20px;margin:20px 0;">
            <h3 style="margin:0 0 8px 0;">{i+1}. {org['name']}</h3>
            <p style="color:#666;margin:0 0 12px 0;">📍 {org['city']} | {org['type']} | ✉️ {contact}</p>
            <p><strong>Subject:</strong> {email['subject']}</p>
            <div style="background:#f9f9f9;padding:15px;border-radius:6px;white-space:pre-wrap;font-size:14px;margin:10px 0;">{email['body']}</div>
            <div style="margin-top:12px;">
                {gmail_link if has_email else '<span style="color:#999;">No email found — visit their website directly</span>'}
            </div>
        </div>
        """

    if call_list:
        html += """<hr/><h2>📞 Call List — no email found, logged to your sheet</h2>"""
        for org in call_list:
            html += f"""
            <div style="border:1px solid #ffcdd2;border-radius:8px;padding:15px;margin:10px 0;background:#fff8f8;">
                <strong>{org['name']}</strong> — {org['type']}<br/>
                📍 {org['city']} | 📞 {org.get('phone', 'Not found')}
                {f"| <a href='{org['website']}'>Website ↗</a>" if org.get('website') else ''}
            </div>"""

    html += "</body></html>"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🔥 Rapid Fire Daily Digest — {len(orgs_and_emails)} emails ready ({current_city})"
    msg["From"] = gmail_user
    msg["To"] = "info@rapidfirecomedytour.org"
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_user, gmail_password)
        server.sendmail(gmail_user, "info@rapidfirecomedytour.org", msg.as_string())

    print(f"Digest email sent with {len(orgs_and_emails)} emails!")

def run_daily_job():
    print(f"Starting daily job at {datetime.now()}")
    try:
        records = get_sheet_data()
        current_city = get_current_city(records)
        contacted_names = [r.get("Organization", "") for r in records if r.get("Organization")]
        print(f"Current city: {current_city} | Already contacted: {len(contacted_names)}")

        orgs = search_orgs(current_city, contacted_names)
        print(f"Found {len(orgs)} new orgs")

        orgs_and_emails = []
        call_list = []
        for org in orgs:
            has_email = org.get("contact") and org["contact"] != "No email found"
            if has_email:
                try:
                    email = generate_email(org)
                    log_to_sheet(org, "Contacted")
                    orgs_and_emails.append((org, email))
                    print(f"Generated email for: {org['name']}")
                except Exception as e:
                    print(f"Error for {org['name']}: {e}")
            else:
                try:
                    phone = find_phone(org)
                    org["phone"] = phone
                    log_to_sheet(org, f"Call directly: {phone}")
                    call_list.append(org)
                    print(f"No email — logged for call: {org['name']} ({phone})")
                except Exception as e:
                    print(f"Error for {org['name']}: {e}")

        send_digest_email(orgs_and_emails, call_list, current_city)
        print("Daily job complete!")

    except Exception as e:
        print(f"Daily job error: {e}")

if __name__ == "__main__":
    run_daily_job()
