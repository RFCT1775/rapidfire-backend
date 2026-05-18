from flask import Flask, jsonify, request
from flask_cors import CORS
import anthropic
import os
import json
import requests
import urllib.parse
import threading
import pg8000.native
from datetime import datetime, timedelta

app = Flask(__name__)
CORS(app, origins="*", allow_headers=["Content-Type"], methods=["GET", "POST", "OPTIONS"])

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
SHEET_URL = os.environ.get("SHEET_URL", "")
HUNTER_API_KEY = os.environ.get("HUNTER_API_KEY", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# County rotation
COUNTIES = [
    "Los Angeles County, CA", "Orange County, CA", "San Diego County, CA",
    "Riverside County, CA", "San Bernardino County, CA", "Ventura County, CA",
    "Santa Barbara County, CA", "Kern County, CA", "Sacramento County, CA",
    "Alameda County, CA", "San Francisco County, CA", "Santa Clara County, CA",
    "Fresno County, CA", "Clark County, NV", "Maricopa County, AZ",
    "Multnomah County, OR", "King County, WA", "Denver County, CO",
    "Dallas County, TX", "Harris County, TX", "Miami-Dade County, FL",
    "Fulton County, GA", "Cook County, IL", "New York County, NY",
]
COUNTY_THRESHOLD = 80

# ─── DATABASE ────────────────────────────────────────────────────────────────

def get_db():
    # Parse DATABASE_URL for pg8000
    url = DATABASE_URL
    # Format: postgresql://user:pass@host:port/dbname
    url = url.replace("postgresql://", "").replace("postgres://", "")
    userpass, hostdbname = url.split("@")
    user, password = userpass.split(":", 1)
    hostport, dbname = hostdbname.split("/", 1)
    if ":" in hostport:
        host, port = hostport.split(":")
        port = int(port)
    else:
        host, port = hostport, 5432
    return pg8000.native.Connection(user=user, password=password, host=host, port=port, database=dbname, ssl_context=True)

def init_db():
    db = get_db()
    db.run("""
        CREATE TABLE IF NOT EXISTS orgs (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            type TEXT,
            city TEXT,
            county TEXT,
            email TEXT,
            website TEXT,
            phone TEXT,
            status TEXT DEFAULT 'Contacted',
            notes TEXT,
            person_spoke_to TEXT,
            date_contacted TIMESTAMP DEFAULT NOW(),
            follow_up_date TIMESTAMP,
            reply_content TEXT,
            hunter_verified BOOLEAN DEFAULT FALSE,
            UNIQUE(name, city)
        )
    """)
    db.run("""
        CREATE TABLE IF NOT EXISTS replies (
            id SERIAL PRIMARY KEY,
            org_name TEXT,
            their_email TEXT,
            reply_content TEXT,
            reply_date TIMESTAMP DEFAULT NOW(),
            follow_up_date TIMESTAMP,
            status TEXT DEFAULT 'New Reply',
            draft_subject TEXT,
            draft_body TEXT
        )
    """)
    db.close()
    print("Database initialized")

def get_contacted_names(county=None):
    db = get_db()
    if county:
        county_short = county.split(",")[0].strip()
        rows = db.run("SELECT name FROM orgs WHERE county ILIKE :county", county=f"%{county_short}%")
    else:
        rows = db.run("SELECT name FROM orgs")
    db.close()
    return [r[0] for r in rows]

def get_current_county():
    db = get_db()
    rows = db.run("SELECT county, COUNT(*) as cnt FROM orgs WHERE status != 'Dead' GROUP BY county")
    db.close()
    counts = {r[0]: r[1] for r in rows if r[0]}
    for county in COUNTIES:
        county_short = county.split(",")[0].strip()
        count = sum(v for k, v in counts.items() if k and county_short.lower() in k.lower())
        if count < COUNTY_THRESHOLD:
            return county
    return COUNTIES[-1]

def save_org(org, status="Contacted"):
    follow_up = datetime.now() + timedelta(days=30)
    try:
        db = get_db()
        db.run("""
            INSERT INTO orgs (name, type, city, county, email, website, status, follow_up_date, hunter_verified)
            VALUES (:name, :type, :city, :county, :email, :website, :status, :follow_up, :hunter)
            ON CONFLICT (name, city) DO UPDATE SET
                status = EXCLUDED.status,
                email = CASE WHEN EXCLUDED.email != 'No email found' THEN EXCLUDED.email ELSE orgs.email END,
                hunter_verified = EXCLUDED.hunter_verified
        """, name=org.get("name"), type=org.get("type"), city=org.get("city"),
            county=org.get("county",""), email=org.get("contact","No email found"),
            website=org.get("website",""), status=status, follow_up=follow_up,
            hunter=org.get("hunter_verified", False))
        db.close()
    except Exception as e:
        print(f"DB save error for {org.get('name')}: {e}")

def update_org_status(name, status, notes="", person="", email=""):
    try:
        db = get_db()
        db.run("""
            UPDATE orgs SET status=:status, notes=:notes, person_spoke_to=:person,
            email=CASE WHEN :email != '' THEN :email ELSE email END
            WHERE name=:name
        """, status=status, notes=notes, person=person, email=email, name=name)
        db.close()
    except Exception as e:
        print(f"DB update error: {e}")

def sync_to_sheet(org, status):
    """Sync to Google Sheets for human visibility"""
    try:
        requests.post(SHEET_URL, json={
            "name": org.get("name"), "type": org.get("type"),
            "city": org.get("city"), "contact": org.get("contact", ""),
            "website": org.get("website", ""), "status": status
        }, timeout=10)
    except Exception as e:
        print(f"Sheet sync error: {e}")

def get_all_orgs(status_filter=None):
    db = get_db()
    if status_filter:
        rows = db.run("SELECT id,name,type,city,county,email,website,phone,status,notes,person_spoke_to,date_contacted,follow_up_date,reply_content,hunter_verified FROM orgs WHERE status ILIKE :f ORDER BY date_contacted DESC", f=f"%{status_filter}%")
    else:
        rows = db.run("SELECT id,name,type,city,county,email,website,phone,status,notes,person_spoke_to,date_contacted,follow_up_date,reply_content,hunter_verified FROM orgs ORDER BY date_contacted DESC")
    db.close()
    cols = ['id','name','type','city','county','email','website','phone','status','notes','person_spoke_to','date_contacted','follow_up_date','reply_content','hunter_verified']
    return [dict(zip(cols, r)) for r in rows]

# ─── HUNTER ──────────────────────────────────────────────────────────────────

def find_email_with_hunter(org_name, website):
    if not HUNTER_API_KEY or not website:
        return None
    try:
        domain = website.replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0].strip()
        if not domain or '.' not in domain:
            return None
        res = requests.get("https://api.hunter.io/v2/domain-search",
            params={"domain": domain, "api_key": HUNTER_API_KEY, "limit": 3}, timeout=10)
        emails = res.json().get("data", {}).get("emails", [])
        if emails:
            return sorted(emails, key=lambda x: x.get("confidence", 0), reverse=True)[0].get("value")
        res2 = requests.get("https://api.hunter.io/v2/email-finder",
            params={"domain": domain, "company": org_name, "api_key": HUNTER_API_KEY}, timeout=10)
        data2 = res2.json().get("data", {})
        email = data2.get("email")
        if email and data2.get("score", 0) > 40:
            return email
        return None
    except Exception as e:
        print(f"Hunter error: {e}")
        return None

# ─── CLAUDE ──────────────────────────────────────────────────────────────────

def clean_text(text):
    return text.replace('\u2014', '').replace('\u2013', '').replace(' - ', ' ').replace('--', '')

def find_phone_number(org_name, org_city, org_website):
    try:
        response = client.messages.create(model="claude-opus-4-5", max_tokens=100,
            messages=[{"role": "user", "content": f'Phone number for {org_name} in {org_city}? Website: {org_website}. Return JSON only: {{"phone":"..."}}. If unknown: {{"phone":"Not found"}}'}])
        text = response.content[0].text.strip()
        if "```" in text: text = text.split("```")[1].split("```")[0].strip()
        if text.startswith("json"): text = text[4:].strip()
        return json.loads(text).get("phone", "Not found")
    except:
        return "Not found"

def search_orgs_with_claude(county, exclude_names):
    county_short = county.split(",")[0].strip()
    state = county.split(",")[1].strip() if "," in county else "CA"
    exclude_str = ""
    if exclude_names:
        exclude_str = "\n\nSkip these already-contacted organizations:\n" + "\n".join(f"- {n}" for n in exclude_names[:80])

    prompt = f"""Find up to 40 real organizations in {county_short}, {state} that would enjoy a live comedy show, guest speaker, or emcee at their events.

Include these types:
- Veterans/military groups: VFW posts, American Legion posts, Marine Corps League, DAV chapters
- First responder groups: firefighter locals/unions, police protective leagues, EMS associations  
- Tactical training companies: veteran-owned firearms training, shooting ranges, self-defense schools
- Executive private security firms: executive protection companies, corporate security consultancies
{exclude_str}

STRICT RULES:
- ONLY include organizations with a known, real website URL starting with http
- If you don't know their website with confidence, skip them
- Do not guess or make up websites
- Quality over quantity

For each org:
- name: exact name
- city: city in {county_short}
- contact: real email only, or "No email found"
- website: real URL starting with http, or skip this org
- type: one of "Fire department", "Police & law enforcement", "EMS & paramedics", "Veterans group", "Tactical training group", "Private security"

Return ONLY a JSON array:
[{{"name":"...","city":"...","contact":"...","website":"...","type":"..."}}]"""

    try:
        response = client.messages.create(model="claude-opus-4-5", max_tokens=6000,
            messages=[{"role": "user", "content": prompt}])
        text = response.content[0].text.strip()
        if "```json" in text: text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text: text = text.split("```")[1].split("```")[0].strip()
        start, end = text.find('['), text.rfind(']') + 1
        if start >= 0 and end > start: text = text[start:end]
        orgs = json.loads(text)
        exclude_lower = [e.lower() for e in exclude_names]
        filtered = [o for o in orgs if
            o.get('name','').lower() not in exclude_lower and
            o.get('website') and
            o['website'].startswith('http') and
            o.get('website') not in ['No website found', 'None', 'N/A']]
        print(f"Claude found {len(orgs)} orgs, {len(filtered)} had valid websites")
        return filtered
    except Exception as e:
        print(f"Search error: {e}")
        return []

def generate_email_text(org_name, org_type, org_city):
    prompt = f"""Write a short outreach email from Michael D'Angelo, founder of Rapid Fire Comedy Tour.

Sound like a real person. Short sentences. Zero dashes of any kind. Get to the point.

Hi there,

My name is Michael D'Angelo. I'm a Marine Corps veteran turned stand-up comedian, which makes more sense than it sounds once you've spent time around Marines.

I run the Rapid Fire Comedy Tour, a nonprofit that brings live stand-up comedy to first responders, military communities, and training groups. The people who deserve a good laugh more than anyone.

I'd love to bring a show to [ORG NAME]. I book comedians for events across the country and I'm also available as a guest speaker or emcee for banquets, graduations, and award ceremonies. If you have an upcoming event and want someone who actually gets your world, I'd love to talk.

You can check out letters of recommendation from previous shows at www.rapidfirecomedytour.org.

Worth a quick call?

Semper Fi,
Michael D'Angelo
Rapid Fire Comedy Tour
info@rapidfirecomedytour.org
www.rapidfirecomedytour.org

RULES: Zero dashes. Never say "free". Under 180 words. Short sentences. No filler. Reference their community.

Organization: {org_name}
Type: {org_type}
City: {org_city}

Return JSON only: {{"subject":"...","body":"..."}}"""

    response = client.messages.create(model="claude-opus-4-5", max_tokens=800,
        messages=[{"role": "user", "content": prompt}])
    text = response.content[0].text.strip()
    if "```json" in text: text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text: text = text.split("```")[1].split("```")[0].strip()
    parsed = json.loads(text)
    parsed['subject'] = clean_text(parsed['subject'])
    parsed['body'] = clean_text(parsed['body'])
    return parsed

def generate_reply_draft(org_name, their_reply, is_followup=False, follow_up_date=""):
    if is_followup:
        prompt = f"""3-4 sentence follow-up from Michael D'Angelo, Rapid Fire Comedy Tour.
{org_name} was interested but we haven't heard back. Follow-up was due {follow_up_date}.
Their reply was: "{their_reply}"
Warm, not pushy. Reference what they said. No dashes.
Return JSON: {{"subject":"...","body":"..."}}"""
    else:
        prompt = f"""3-4 sentence reply from Michael D'Angelo, Rapid Fire Comedy Tour.
They said: "{their_reply}"
Respond proportionally. If they mentioned a future event, acknowledge it and leave the ball in their court.
No dashes. Brief and human.
Return JSON: {{"subject":"...","body":"..."}}"""

    response = client.messages.create(model="claude-opus-4-5", max_tokens=300,
        messages=[{"role": "user", "content": prompt}])
    text = response.content[0].text.strip()
    if "```json" in text: text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text: text = text.split("```")[1].split("```")[0].strip()
    parsed = json.loads(text)
    parsed['subject'] = clean_text(parsed['subject'])
    parsed['body'] = clean_text(parsed['body'])
    return parsed

# ─── DIGEST EMAIL ─────────────────────────────────────────────────────────────

def send_digest_email(orgs_and_emails, call_list, current_county, reply_drafts=[]):
    sendgrid_key = os.environ.get("SENDGRID_API_KEY")
    if not sendgrid_key:
        print("SendGrid key not set")
        return False

    html = f"""<html><body style="font-family:Arial,sans-serif;max-width:800px;margin:0 auto;padding:20px;">
    <h1 style="color:#ff4d00;">Rapid Fire Daily Digest</h1>
    <p style="color:#666;">{datetime.now().strftime("%B %d, %Y")} | {current_county}</p>
    <p>Emails ready: {len(orgs_and_emails)} | Call list: {len(call_list)} | Replies: {len(reply_drafts)}</p>"""

    if reply_drafts:
        html += "<hr/><h2 style='color:#22c55e;'>Replies to Action</h2>"
        for rd in reply_drafts:
            is_fu = rd['status'] == 'Follow-up needed'
            color = "#f97316" if is_fu else "#22c55e"
            subj = urllib.parse.quote(rd['draft']['subject'])
            body = urllib.parse.quote(rd['draft']['body'])
            link = f'<a href="https://mail.google.com/mail/?view=cm&fs=1&to={rd["their_email"]}&su={subj}&body={body}&from=info@rapidfirecomedytour.org" style="background:{color};color:white;padding:8px 16px;border-radius:6px;text-decoration:none;font-weight:bold;">Reply in Gmail</a>'
            html += f"""<div style="border:2px solid {color};border-radius:8px;padding:20px;margin:15px 0;background:#f0fff4;">
                <h3>{rd['org']}</h3>
                <p style="color:#666;font-size:13px;">"{rd['reply_content'][:200]}"</p>
                <p><strong>Subject:</strong> {rd['draft']['subject']}</p>
                <div style="background:#f9f9f9;padding:12px;border-radius:6px;white-space:pre-wrap;font-size:13px;margin:8px 0;">{rd['draft']['body']}</div>
                {link}
            </div>"""

    html += "<hr/><h2>New Outreach Emails</h2>"
    for i, (org, email) in enumerate(orgs_and_emails):
        contact = org.get("contact", "")
        subj = urllib.parse.quote(email["subject"])
        body = urllib.parse.quote(email["body"])
        verified = ' <span style="color:#22c55e;font-size:11px;">✓ Hunter verified</span>' if org.get("hunter_verified") else ''
        link = f'<a href="https://mail.google.com/mail/?view=cm&fs=1&to={contact}&su={subj}&body={body}&from=info@rapidfirecomedytour.org" style="background:#ff4d00;color:white;padding:8px 16px;border-radius:6px;text-decoration:none;font-weight:bold;">Open in Gmail</a>'
        html += f"""<div style="border:1px solid #ddd;border-radius:8px;padding:20px;margin:15px 0;">
            <h3>{i+1}. {org['name']}{verified}</h3>
            <p style="color:#666;">{org['city']} | {org['type']} | {contact}</p>
            <p><strong>Subject:</strong> {email['subject']}</p>
            <div style="background:#f9f9f9;padding:15px;border-radius:6px;white-space:pre-wrap;font-size:14px;margin:10px 0;">{email['body']}</div>
            {link}
        </div>"""

    if call_list:
        html += "<hr/><h2>Call List</h2>"
        for org in call_list:
            phone = org.get("phone", "Not found")
            search_q = urllib.parse.quote(f"{org['name']} {org['city']} phone number")
            phone_display = f'<a href="https://www.google.com/search?q={search_q}" style="color:#ff4d00;">Find on Google</a>' if phone == "Not found" else f"Phone: {phone}"
            html += f"""<div style="border:1px solid #ffcdd2;border-radius:8px;padding:15px;margin:10px 0;background:#fff8f8;">
                <strong>{org['name']}</strong> - {org['type']}<br/>
                {org['city']} | {phone_display}
                {f'| <a href="{org["website"]}">Website</a>' if org.get('website') else ''}
            </div>"""

    html += "</body></html>"

    res = requests.post("https://api.sendgrid.com/v3/mail/send",
        headers={"Authorization": f"Bearer {sendgrid_key}", "Content-Type": "application/json"},
        json={"personalizations": [{"to": [{"email": "info@rapidfirecomedytour.org"}]}],
              "from": {"email": "info@rapidfirecomedytour.org", "name": "Rapid Fire Comedy Tour"},
              "subject": f"Rapid Fire Digest - {len(orgs_and_emails)} emails ready ({current_county})",
              "content": [{"type": "text/html", "value": html}]}, timeout=30)

    if res.status_code in (200, 202):
        print(f"Digest sent! {len(orgs_and_emails)} emails, {len(call_list)} calls, {len(reply_drafts)} replies")
        return True
    else:
        print(f"SendGrid error: {res.status_code} - {res.text}")
        return False

# ─── DAILY JOB ───────────────────────────────────────────────────────────────

def run_daily_background():
    try:
        print(f"Daily job starting at {datetime.now()}")
        current_county = get_current_county()
        contacted_names = get_contacted_names()
        print(f"County: {current_county} | DB has {len(contacted_names)} orgs")

        # Get reply drafts
        reply_drafts = []
        try:
            db = get_db()
            rows = db.run("SELECT org_name,their_email,reply_content,follow_up_date,status FROM replies WHERE status IN ('New Reply','Follow-up needed')")
            db.close()
            cols = ['org_name','their_email','reply_content','follow_up_date','status']
            pending_replies = [dict(zip(cols, r)) for r in rows]
            for r in pending_replies:
                is_fu = r['status'] == 'Follow-up needed'
                draft = generate_reply_draft(r['org_name'], r['reply_content'] or '', is_fu, str(r.get('follow_up_date','')))
                reply_drafts.append({"org": r['org_name'], "their_email": r['their_email'],
                    "reply_content": r['reply_content'] or '', "follow_up_date": str(r.get('follow_up_date','')),
                    "status": r['status'], "draft": draft})
        except Exception as e:
            print(f"Reply draft error: {e}")

        orgs = search_orgs_with_claude(current_county, contacted_names)
        print(f"Found {len(orgs)} new orgs")

        orgs_and_emails = []
        call_list = []

        for org in orgs:
            org['county'] = current_county

            # Hunter first on every org with a website
            if org.get("website"):
                hunter_email = find_email_with_hunter(org['name'], org['website'])
                if hunter_email:
                    org["contact"] = hunter_email
                    org["hunter_verified"] = True
                    print(f"Hunter: {org['name']} -> {hunter_email}")

            has_email = org.get("contact") and org["contact"] != "No email found"

            if has_email:
                try:
                    email = generate_email_text(org['name'], org['type'], org['city'])
                    save_org(org, "Contacted")
                    sync_to_sheet(org, "Contacted")
                    orgs_and_emails.append((org, email))
                    print(f"Email ready: {org['name']}")
                except Exception as e:
                    print(f"Email error: {e}")
            else:
                try:
                    phone = find_phone_number(org['name'], org['city'], org.get('website',''))
                    org["phone"] = phone
                    search_q = urllib.parse.quote(f"{org['name']} {org['city']} phone number")
                    status = f"Call directly: {phone}" if phone != "Not found" else f"Call directly - google.com/search?q={search_q}"
                    save_org(org, status)
                    sync_to_sheet(org, status)
                    call_list.append(org)
                    print(f"Call list: {org['name']} ({phone})")
                except Exception as e:
                    print(f"Call error: {e}")

        send_digest_email(orgs_and_emails, call_list, current_county, reply_drafts)
        print("Daily job complete!")

    except Exception as e:
        print(f"Daily job error: {e}")

# ─── ROUTES ──────────────────────────────────────────────────────────────────

@app.route('/run-daily', methods=['GET', 'POST'])
def run_daily():
    thread = threading.Thread(target=run_daily_background)
    thread.daemon = True
    thread.start()
    return jsonify({'success': True, 'message': 'Daily job started'})

@app.route('/init-db', methods=['GET'])
def init_db_route():
    try:
        init_db()
        return jsonify({'success': True, 'message': 'Database initialized'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/dashboard-data', methods=['GET'])
def dashboard_data():
    try:
        orgs = get_all_orgs()
        total = len(orgs)
        contacted = sum(1 for o in orgs if o['status'] == 'Contacted')
        bounced = sum(1 for o in orgs if 'Bounced' in str(o['status']))
        replied = sum(1 for o in orgs if 'Replied' in str(o['status']))
        follow_up = sum(1 for o in orgs if 'Follow-up needed' in str(o['status']))
        call_only = sum(1 for o in orgs if 'Call directly' in str(o['status']))
        dead = sum(1 for o in orgs if 'Dead' in str(o['status']))

        need_contact = [{"name": o['name'], "type": o['type'], "city": o['city'],
            "email": o['email'], "website": o['website'], "status": o['status'],
            "phone": o['phone'], "date_contacted": str(o['date_contacted']),
            "follow_up_date": str(o['follow_up_date'])}
            for o in orgs if 'Call directly' in str(o['status']) or 'Bounced' in str(o['status'])]

        replies = [{"name": o['name'], "email": o['email'], "status": o['status'],
            "reply_content": o['reply_content'], "follow_up_date": str(o['follow_up_date'])}
            for o in orgs if 'Replied' in str(o['status']) or 'Follow-up' in str(o['status'])]

        return jsonify({"success": True,
            "stats": {"total": total, "contacted": contacted, "bounced": bounced,
                      "replied": replied, "follow_up_needed": follow_up, "call_only": call_only, "dead": dead},
            "need_contact": need_contact, "replies": replies, "records": [
                {"Organization": o['name'], "Type": o['type'], "City": o['city'],
                 "Email": o['email'], "Website": o['website'], "Status": o['status'],
                 "Date Contacted": str(o['date_contacted']), "Follow-up Date": str(o['follow_up_date'])}
                for o in orgs]})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/update-contact', methods=['POST'])
def update_contact():
    try:
        data = request.get_json()
        update_org_status(data.get('name'), data.get('status'), 
                         data.get('notes',''), data.get('person',''), data.get('email',''))
        sync_to_sheet(data, data.get('status'))
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/rehunt', methods=['GET', 'POST'])
def rehunt():
    def rehunt_bg():
        try:
            orgs = get_all_orgs()
            updated = 0
            for o in orgs:
                if updated >= 45: break
                status = str(o.get('status',''))
                if 'Dead' in status or 'Booked' in status: continue
                email = o.get('email','')
                website = o.get('website','')
                if (not email or email == 'No email found' or 'Bounced' in status or 'Call directly' in status) and website:
                    hunter_email = find_email_with_hunter(o['name'], website)
                    if hunter_email:
                        update_org_status(o['name'], 'Contacted', email=hunter_email)
                        sync_to_sheet({'name': o['name'], 'type': o['type'], 'city': o['city'],
                            'website': website, 'contact': hunter_email}, 'Email found via Hunter')
                        updated += 1
                        print(f"Hunter updated: {o['name']} -> {hunter_email}")
            print(f"Rehunt done. Updated: {updated}")
        except Exception as e:
            print(f"Rehunt error: {e}")
    thread = threading.Thread(target=rehunt_bg)
    thread.daemon = True
    thread.start()
    return jsonify({'success': True, 'message': 'Hunter re-scrape started'})

@app.route('/search', methods=['GET', 'POST'])
def search():
    if request.method == 'POST':
        data = request.get_json() or {}
        location = data.get('location', 'Los Angeles, CA')
        exclude_names = data.get('exclude', [])
    else:
        location = request.args.get('location', 'Los Angeles, CA')
        exclude_names = []
    try:
        orgs = search_orgs_with_claude(location, exclude_names)
        return jsonify({'success': True, 'orgs': orgs})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/generate-email', methods=['POST'])
def generate_email():
    try:
        data = request.get_json()
        email = generate_email_text(data.get('name',''), data.get('type',''), data.get('city',''))
        return jsonify({'success': True, 'subject': email['subject'], 'body': email['body']})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/find-phone', methods=['POST'])
def find_phone():
    try:
        data = request.get_json()
        phone = find_phone_number(data.get('name',''), data.get('city',''), data.get('website',''))
        return jsonify({'success': True, 'phone': phone})
    except Exception as e:
        return jsonify({'success': False, 'phone': 'Not found'}), 500

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'message': 'Rapid Fire backend is running!'})

if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
