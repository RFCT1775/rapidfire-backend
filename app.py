from flask import Flask, jsonify, request
from flask_cors import CORS
import anthropic
import os
import json
import smtplib
import requests
import urllib.parse
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timedelta

app = Flask(__name__)
CORS(app, origins="*", allow_headers=["Content-Type"], methods=["GET", "POST", "OPTIONS"])

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
SHEET_URL = os.environ.get("SHEET_URL", "")
GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")

CITIES = [
    "Los Angeles, CA", "Orange County, CA", "San Diego, CA",
    "Riverside, CA", "San Bernardino, CA", "Ventura, CA",
    "Sacramento, CA", "San Francisco, CA", "Fresno, CA", "Bakersfield, CA",
    "Las Vegas, NV", "Phoenix, AZ", "Portland, OR", "Seattle, WA",
    "Denver, CO", "Dallas, TX", "Houston, TX", "Miami, FL",
    "Atlanta, GA", "Chicago, IL", "New York, NY",
]
CITY_THRESHOLD = 40

def clean_text(text):
    """Remove em-dashes and replace with regular dashes or nothing"""
    return text.replace('\u2014', '-').replace('\u2013', '-').replace('--', '-')

def search_orgs_with_claude(location, org_type, exclude_names):
    city = location.split(',')[0].strip()
    state = location.split(',')[1].strip() if ',' in location else 'CA'

    exclude_str = ""
    if exclude_names:
        exclude_str = f"\n\nDo NOT include any of these already-contacted organizations:\n" + "\n".join(f"- {n}" for n in exclude_names[:50])

    if org_type == 'all':
        counts = "exactly 5 veterans groups, exactly 5 first responder groups (fire/police/EMS), exactly 5 tactical training companies, and exactly 5 executive private security companies"
        type_desc = """Include these specific types:
- Veterans groups: VFW posts, American Legion posts, veteran service organizations, military fraternal groups
- First responder groups: firefighter locals/unions, police protective leagues, EMS associations, benevolent societies
- Tactical training companies: veteran-owned firearms training companies, self-defense schools, tactical training groups, shooting ranges that host community events
- Executive private security companies: high-end security firms, executive protection companies, corporate security consultancies staffed by former military or law enforcement"""
    elif org_type == 'veterans':
        counts = "10 veterans groups"
        type_desc = "Veterans groups: VFW posts, American Legion posts, veteran service organizations, military fraternal groups"
    elif org_type == 'tactical':
        counts = "10 tactical training companies"
        type_desc = "Tactical training companies: veteran-owned firearms training companies, self-defense schools, tactical training groups, shooting ranges"
    elif org_type == 'security':
        counts = "10 executive private security companies"
        type_desc = "Executive private security companies: high-end security firms, executive protection companies, corporate security consultancies staffed by former military or law enforcement"
    else:
        counts = "10 first responder organizations"
        type_desc = "First responder groups: firefighter locals/unions, police protective leagues, EMS associations, benevolent societies"

    prompt = f"""You are helping find organizations in {location} that might want to book a live comedy show, guest speaker, emcee, or entertainment for their next event.

Find {counts} in or near {city}, {state}.

{type_desc}

Focus on groups that host events, parties, banquets, graduations, and social gatherings.
{exclude_str}

For each organization:
- Real name
- City
- Contact email: only include if confident it is real. If unsure, write "No email found"
- Website if known
- Type: use exactly one of: "Fire department", "Police & law enforcement", "EMS & paramedics", "Veterans group", "Tactical training group", "Private security"

Return ONLY a valid JSON array:
[{{"name":"...","city":"{city}","contact":"...","website":"...","type":"..."}}]"""

    try:
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
        start = text.find('[')
        end = text.rfind(']') + 1
        if start >= 0 and end > start:
            text = text[start:end]
        orgs = json.loads(text)
        exclude_lower = [e.lower() for e in exclude_names]
        return [o for o in orgs if o.get('name','').lower() not in exclude_lower]
    except Exception as e:
        print(f"Search error: {e}")
        return []

def generate_email_text(org_name, org_type, org_city):
    prompt = f"""Write a warm, personal outreach email from Michael D'Angelo, founder of Rapid Fire Comedy Tour.

Use this as your guide but personalize it for the specific org:

Hi there,

My name is Michael D'Angelo. I'm a Marine Corps machine gunner turned stand-up comedian, which is probably the only career change that makes less sense than it sounds.

I started the Rapid Fire Comedy Tour to bring live comedy to the people who spend their days running toward danger. First responders, military, training communities - the folks who deserve a good laugh more than anyone.

I'd love to bring a show to [ORG NAME]. I book comedians for events across the country, and there's something special about making a room full of [THEIR COMMUNITY] laugh. I'm also available as a guest speaker or emcee for banquets, graduations, award ceremonies, or any event where you want someone who actually gets your world.

If you have an upcoming event, holiday party, or awards banquet, I'd love to have a conversation about making it memorable. You can visit www.rapidfirecomedytour.org to read letters of recommendation from previous shows.

Worth a conversation?

Semper Fi,
Michael D'Angelo
Founder, Rapid Fire Comedy Tour
info@rapidfirecomedytour.org
www.rapidfirecomedytour.org

STRICT RULES:
- Never use em-dashes (the long dash like this: -). Use a regular hyphen or rewrite the sentence instead.
- Never use the word "free"
- Keep it under 200 words
- Write like a real person, not a marketer
- Reference their specific community naturally

Organization: {org_name}
Type: {org_type}
City: {org_city}

Return JSON only, no markdown:
{{"subject": "...", "body": "..."}}"""

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
    parsed = json.loads(text)
    parsed['subject'] = clean_text(parsed['subject'])
    parsed['body'] = clean_text(parsed['body'])
    return parsed

def find_phone_number(org_name, org_city, org_website):
    try:
        prompt = f"""What is the main phone number for {org_name} in {org_city}?
{f"Their website is {org_website}." if org_website else ""}
Return ONLY JSON: {{"phone": "..."}}
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
        return json.loads(text).get("phone", "Not found")
    except:
        return "Not found"

def get_sheet_data():
    try:
        res = requests.get(SHEET_URL, timeout=15)
        data = res.json()
        return data.get("records", [])
    except Exception as e:
        print(f"Error getting sheet data: {e}")
        return []

def log_org_to_sheet(org, status="Contacted"):
    try:
        requests.post(SHEET_URL, json={
            "name": org["name"], "type": org["type"],
            "city": org["city"], "contact": org.get("contact", ""),
            "website": org.get("website", ""), "status": status
        }, timeout=15)
    except Exception as e:
        print(f"Sheet log error: {e}")

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

def send_digest_email(orgs_and_emails, call_list, current_city):
    if not GMAIL_USER or not GMAIL_PASSWORD:
        print("Gmail credentials not set")
        return False

    html = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:800px;margin:0 auto;padding:20px;">
    <h1 style="color:#ff4d00;">Rapid Fire Daily Outreach Digest</h1>
    <p style="color:#666;">Date: {datetime.now().strftime("%B %d, %Y")} | City: {current_city}</p>
    <p>Emails ready to send: {len(orgs_and_emails)} | Orgs to call: {len(call_list)}</p>
    <hr/>
    <h2>Emails - click to open in Gmail and send</h2>"""

    for i, (org, email) in enumerate(orgs_and_emails):
        contact = org.get("contact", "")
        subject = urllib.parse.quote(email["subject"])
        body = urllib.parse.quote(email["body"])
        gmail_link = f'<a href="https://mail.google.com/mail/?view=cm&fs=1&to={contact}&su={subject}&body={body}&from=info@rapidfirecomedytour.org" style="background:#ff4d00;color:white;padding:8px 16px;border-radius:6px;text-decoration:none;font-weight:bold;">Open in Gmail</a>'
        html += f"""
        <div style="border:1px solid #ddd;border-radius:8px;padding:20px;margin:15px 0;">
            <h3 style="margin:0 0 8px 0;">{i+1}. {org['name']}</h3>
            <p style="color:#666;margin:0 0 10px 0;">Location: {org['city']} | Type: {org['type']} | Email: {contact}</p>
            <p><strong>Subject:</strong> {email['subject']}</p>
            <div style="background:#f9f9f9;padding:15px;border-radius:6px;white-space:pre-wrap;font-size:14px;margin:10px 0;">{email['body']}</div>
            <div style="margin-top:12px;">{gmail_link}</div>
        </div>"""

    if call_list:
        html += "<hr/><h2>Call List - no email found</h2>"
        for org in call_list:
            phone = org.get("phone", "Not found")
            if phone == "Not found":
                search_query = urllib.parse.quote(f"{org['name']} {org['city']} phone number")
                phone_display = f'<a href="https://www.google.com/search?q={search_query}" style="color:#ff4d00;">Find on Google</a>'
            else:
                phone_display = f'Phone: {phone}'
            html += f"""
            <div style="border:1px solid #ffcdd2;border-radius:8px;padding:15px;margin:10px 0;background:#fff8f8;">
                <strong>{org['name']}</strong> - {org['type']}<br/>
                Location: {org['city']} | {phone_display}
                {f'| <a href="{org["website"]}">Website</a>' if org.get('website') else ''}
            </div>"""

    html += "</body></html>"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Rapid Fire Daily Digest - {len(orgs_and_emails)} emails ready ({current_city})"
    msg["From"] = GMAIL_USER
    msg["To"] = "info@rapidfirecomedytour.org"
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_PASSWORD)
        server.sendmail(GMAIL_USER, "info@rapidfirecomedytour.org", msg.as_string())

    print(f"Digest sent with {len(orgs_and_emails)} emails and {len(call_list)} call leads!")
    return True

import threading

def run_daily_background():
    try:
        print(f"Starting daily job at {datetime.now()}")
        records = get_sheet_data()
        current_city = get_current_city(records)
        contacted_names = [r.get("Organization", "") for r in records if r.get("Organization")]
        print(f"City: {current_city} | Contacted: {len(contacted_names)}")

        orgs = search_orgs_with_claude(current_city, 'all', contacted_names)
        print(f"Found {len(orgs)} new orgs")

        orgs_and_emails = []
        call_list = []

        for org in orgs:
            has_email = org.get("contact") and org["contact"] != "No email found"
            if has_email:
                try:
                    email = generate_email_text(org['name'], org['type'], org['city'])
                    log_org_to_sheet(org, "Contacted")
                    orgs_and_emails.append((org, email))
                    print(f"Email generated: {org['name']}")
                except Exception as e:
                    print(f"Email error for {org['name']}: {e}")
            else:
                try:
                    phone = find_phone_number(org['name'], org['city'], org.get('website', ''))
                    org["phone"] = phone
                    if phone == "Not found":
                        search_query = urllib.parse.quote(f"{org['name']} {org['city']} phone number")
                        status = f"Call directly - google.com/search?q={search_query}"
                    else:
                        status = f"Call directly: {phone}"
                    log_org_to_sheet(org, status)
                    call_list.append(org)
                    print(f"Call logged: {org['name']} ({phone})")
                except Exception as e:
                    print(f"Call log error for {org['name']}: {e}")

        send_digest_email(orgs_and_emails, call_list, current_city)
        print("Daily job complete!")

    except Exception as e:
        print(f"Daily job error: {e}")

@app.route('/run-daily', methods=['POST', 'GET'])
def run_daily():
    thread = threading.Thread(target=run_daily_background)
    thread.daemon = True
    thread.start()
    return jsonify({'success': True, 'message': 'Daily job started in background'})

@app.route('/search', methods=['GET', 'POST'])
def search():
    if request.method == 'POST':
        data = request.get_json() or {}
        location = data.get('location', 'Los Angeles, CA')
        org_type = data.get('type', 'all')
        exclude_names = data.get('exclude', [])
    else:
        location = request.args.get('location', 'Los Angeles, CA')
        org_type = request.args.get('type', 'all')
        exclude_names = []
    try:
        orgs = search_orgs_with_claude(location, org_type, exclude_names)
        return jsonify({'success': True, 'orgs': orgs})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/generate-email', methods=['POST'])
def generate_email():
    try:
        data = request.get_json()
        email = generate_email_text(data.get('name', ''), data.get('type', ''), data.get('city', ''))
        return jsonify({'success': True, 'subject': email['subject'], 'body': email['body']})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/find-phone', methods=['POST'])
def find_phone():
    try:
        data = request.get_json()
        phone = find_phone_number(data.get('name', ''), data.get('city', ''), data.get('website', ''))
        return jsonify({'success': True, 'phone': phone})
    except Exception as e:
        return jsonify({'success': False, 'phone': 'Not found'}), 500

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'message': 'Rapid Fire backend is running!'})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
