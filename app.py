from flask import Flask, jsonify, request
from flask_cors import CORS
import anthropic
import os
import json
import requests
import urllib.parse
import threading
from datetime import datetime, timedelta

app = Flask(__name__)
CORS(app, origins="*", allow_headers=["Content-Type"], methods=["GET", "POST", "OPTIONS"])

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
SHEET_URL = os.environ.get("SHEET_URL", "")
HUNTER_API_KEY = os.environ.get("HUNTER_API_KEY", "")

# County rotation order
COUNTIES = [
    "Los Angeles County, CA",
    "Orange County, CA",
    "San Diego County, CA",
    "Riverside County, CA",
    "San Bernardino County, CA",
    "Ventura County, CA",
    "Santa Barbara County, CA",
    "Kern County, CA",
    "Sacramento County, CA",
    "Alameda County, CA",
    "San Francisco County, CA",
    "Santa Clara County, CA",
    "Fresno County, CA",
    "Clark County, NV",
    "Maricopa County, AZ",
    "Multnomah County, OR",
    "King County, WA",
    "Denver County, CO",
    "Dallas County, TX",
    "Harris County, TX",
    "Miami-Dade County, FL",
    "Fulton County, GA",
    "Cook County, IL",
    "New York County, NY",
]

COUNTY_THRESHOLD = 80  # Move to next county after this many contacts

def clean_text(text):
    return text.replace('\u2014', '').replace('\u2013', '').replace(' - ', ' ').replace('--', '')

def find_email_with_hunter(org_name, website):
    if not HUNTER_API_KEY or not website:
        return None
    try:
        domain = website.replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0].strip()
        if not domain or '.' not in domain:
            return None

        # Try domain search first
        res = requests.get(
            "https://api.hunter.io/v2/domain-search",
            params={"domain": domain, "api_key": HUNTER_API_KEY, "limit": 3},
            timeout=10
        )
        data = res.json()
        emails = data.get("data", {}).get("emails", [])
        if emails:
            # Return highest confidence email
            best = sorted(emails, key=lambda x: x.get("confidence", 0), reverse=True)
            return best[0].get("value")

        # Try email finder
        res2 = requests.get(
            "https://api.hunter.io/v2/email-finder",
            params={"domain": domain, "company": org_name, "api_key": HUNTER_API_KEY},
            timeout=10
        )
        data2 = res2.json()
        email = data2.get("data", {}).get("email")
        score = data2.get("data", {}).get("score", 0)
        if email and score > 40:
            return email
        return None
    except Exception as e:
        print(f"Hunter error for {org_name}: {e}")
        return None

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
        return data  # Return full response including replies
    except Exception as e:
        print(f"Error getting sheet data: {e}")
        return {"records": [], "replies": []}

def get_records(data):
    if isinstance(data, list):
        return data
    return data.get("records", [])

def log_org_to_sheet(org, status="Contacted"):
    try:
        requests.post(SHEET_URL, json={
            "name": org["name"], "type": org["type"],
            "city": org["city"], "contact": org.get("contact", ""),
            "website": org.get("website", ""), "status": status
        }, timeout=15)
    except Exception as e:
        print(f"Sheet log error: {e}")

def get_current_county(records_or_data):
    if isinstance(records_or_data, dict):
        records = records_or_data.get("records", [])
    else:
        records = records_or_data
    county_counts = {}
    for row in records:
        city = row.get("City", "")
        if city:
            county_counts[city] = county_counts.get(city, 0) + 1

    for county in COUNTIES:
        county_short = county.split(",")[0].strip()
        count = sum(v for k, v in county_counts.items() if county_short.lower() in k.lower())
        if count < COUNTY_THRESHOLD:
            return county
    return COUNTIES[-1]

def search_orgs_with_claude(county, org_type, exclude_names):
    county_short = county.split(",")[0].strip()
    state = county.split(",")[1].strip() if "," in county else "CA"

    exclude_str = ""
    if exclude_names:
        exclude_str = "\n\nDo NOT include these already-contacted organizations:\n" + "\n".join(f"- {n}" for n in exclude_names[:60])

    if org_type == 'all':
        counts = "exactly 10 veterans/military groups, exactly 10 first responder groups, exactly 10 tactical training companies, and exactly 10 executive private security companies"
        type_desc = """Types to find:
- Veterans/military: VFW posts, American Legion posts, Marine Corps League, DAV chapters, veteran service organizations
- First responders: firefighter locals/unions, police protective leagues, sheriff associations, EMS unions, benevolent societies
- Tactical training: veteran-owned firearms training, self-defense schools, shooting ranges, tactical training companies
- Executive private security: executive protection firms, corporate security consultancies, high-end security companies staffed by former military or law enforcement"""
    elif org_type == 'veterans':
        counts = "20 veterans and military organizations"
        type_desc = "Veterans/military groups: VFW posts, American Legion posts, Marine Corps League, DAV chapters, veteran service organizations"
    elif org_type == 'tactical':
        counts = "20 tactical training companies"
        type_desc = "Tactical training: veteran-owned firearms training companies, self-defense schools, shooting ranges"
    elif org_type == 'security':
        counts = "20 executive private security companies"
        type_desc = "Executive private security: executive protection firms, corporate security consultancies staffed by former military or law enforcement"
    else:
        counts = "20 first responder organizations"
        type_desc = "First responders: firefighter locals/unions, police protective leagues, EMS associations, benevolent societies"

    prompt = f"""Find {counts} in {county_short}, {state}.

{type_desc}

Focus on groups that host events, banquets, parties, graduations, and social gatherings.
{exclude_str}

For each organization provide:
- name: exact organization name
- city: city within {county_short}
- contact: ONLY include if you are confident it is a real working email. Otherwise write "No email found". Do not guess.
- website: their website URL if you know it
- type: exactly one of "Fire department", "Police & law enforcement", "EMS & paramedics", "Veterans group", "Tactical training group", "Private security"

Return ONLY a valid JSON array:
[{{"name":"...","city":"...","contact":"...","website":"...","type":"..."}}]"""

    try:
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=5000,
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
    prompt = f"""Write a short outreach email from Michael D'Angelo, founder of Rapid Fire Comedy Tour.

Sound like a real person texting from their phone. Short sentences. No dashes of any kind. Conversational and genuine. Get to the point fast.

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

RULES:
- Zero dashes. Not long ones, not short ones. Rewrite any sentence that needs one.
- Never use the word "free"
- Under 180 words
- Two to three short sentences per paragraph max
- No filler like "I hope this finds you well"
- Reference their specific community naturally

Organization: {org_name}
Type: {org_type}
City: {org_city}

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
    parsed = json.loads(text)
    parsed['subject'] = clean_text(parsed['subject'])
    parsed['body'] = clean_text(parsed['body'])
    return parsed

def send_digest_email(orgs_and_emails, call_list, current_county, reply_drafts=[]):
    sendgrid_key = os.environ.get("SENDGRID_API_KEY")
    if not sendgrid_key:
        print("SendGrid API key not set")
        return False

    html = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:800px;margin:0 auto;padding:20px;">
    <h1 style="color:#ff4d00;">Rapid Fire Daily Outreach Digest</h1>
    <p style="color:#666;">Date: {datetime.now().strftime("%B %d, %Y")} | County: {current_county}</p>
    <p>Emails ready: {len(orgs_and_emails)} | Call list: {len(call_list)} | Replies to action: {len(reply_drafts)}</p>"""

    # Reply drafts section FIRST - most important
    if reply_drafts:
        html += """<hr/><h2 style="color:#22c55e;">Replies to Action</h2>"""
        for rd in reply_drafts:
            is_followup = rd['status'] == 'Follow-up needed'
            label = "Follow-up Needed" if is_followup else "New Reply"
            color = "#ff9800" if is_followup else "#22c55e"
            subject = urllib.parse.quote(rd['draft']['subject'])
            body = urllib.parse.quote(rd['draft']['body'])
            gmail_link = f'<a href="https://mail.google.com/mail/?view=cm&fs=1&to={rd["their_email"]}&su={subject}&body={body}&from=info@rapidfirecomedytour.org" style="background:#22c55e;color:white;padding:8px 16px;border-radius:6px;text-decoration:none;font-weight:bold;">Reply in Gmail</a>'
            html += f"""
            <div style="border:2px solid {color};border-radius:8px;padding:20px;margin:15px 0;background:#f0fff4;">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
                    <h3 style="margin:0;">{rd['org']}</h3>
                    <span style="background:{color};color:white;padding:3px 10px;border-radius:20px;font-size:12px;">{label}</span>
                </div>
                <p style="color:#666;font-size:13px;margin:0 0 10px 0;">Their reply: <em>"{rd['reply_content'][:200]}..."</em></p>
                {'<p style="color:#ff9800;font-size:13px;">Follow-up was due: ' + rd['follow_up_date'] + '</p>' if is_followup else ''}
                <p><strong>Subject:</strong> {rd['draft']['subject']}</p>
                <div style="background:#f9f9f9;padding:15px;border-radius:6px;white-space:pre-wrap;font-size:14px;margin:10px 0;">{rd['draft']['body']}</div>
                <div style="margin-top:12px;">{gmail_link}</div>
            </div>"""

    html += "<hr/><h2>New Outreach Emails</h2>"

    for i, (org, email) in enumerate(orgs_and_emails):
        contact = org.get("contact", "")
        subject = urllib.parse.quote(email["subject"])
        body = urllib.parse.quote(email["body"])
        gmail_link = f'<a href="https://mail.google.com/mail/?view=cm&fs=1&to={contact}&su={subject}&body={body}&from=info@rapidfirecomedytour.org" style="background:#ff4d00;color:white;padding:8px 16px;border-radius:6px;text-decoration:none;font-weight:bold;">Open in Gmail</a>'
        verified = ' <span style="color:#22c55e;font-size:11px;">Hunter verified</span>' if org.get("hunter_verified") else ''
        html += f"""
        <div style="border:1px solid #ddd;border-radius:8px;padding:20px;margin:15px 0;">
            <h3 style="margin:0 0 8px 0;">{i+1}. {org['name']}{verified}</h3>
            <p style="color:#666;margin:0 0 10px 0;">{org['city']} | {org['type']} | {contact}</p>
            <p><strong>Subject:</strong> {email['subject']}</p>
            <div style="background:#f9f9f9;padding:15px;border-radius:6px;white-space:pre-wrap;font-size:14px;margin:10px 0;">{email['body']}</div>
            <div style="margin-top:12px;">{gmail_link}</div>
        </div>"""

    if call_list:
        html += "<hr/><h2>Call List</h2>"
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
                {org['city']} | {phone_display}
                {f'| <a href="{org["website"]}">Website</a>' if org.get('website') else ''}
            </div>"""

    html += "</body></html>"

    payload = {
        "personalizations": [{"to": [{"email": "info@rapidfirecomedytour.org"}]}],
        "from": {"email": "info@rapidfirecomedytour.org", "name": "Rapid Fire Comedy Tour"},
        "subject": f"Rapid Fire Daily Digest - {len(orgs_and_emails)} emails ready ({current_county})",
        "content": [{"type": "text/html", "value": html}]
    }

    response = requests.post(
        "https://api.sendgrid.com/v3/mail/send",
        headers={"Authorization": f"Bearer {sendgrid_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=30
    )

    if response.status_code in (200, 202):
        print(f"Digest sent! Emails: {len(orgs_and_emails)} | Calls: {len(call_list)}")
        return True
    else:
        print(f"SendGrid error: {response.status_code} - {response.text}")
        return False

def get_reply_drafts(records_data):
    """Get replies that need follow-up drafts from the Replies sheet"""
    replies = records_data.get("replies", [])
    drafts = []
    
    for reply in replies:
        status = reply.get("status", "")
        if status not in ["New Reply", "Follow-up needed"]:
            continue
            
        org_name = reply.get("Organization", "")
        their_email = reply.get("Their Email", "") or reply.get("email", "")
        reply_content = reply.get("Reply Content", "") or reply.get("reply content", "")
        follow_up_date = reply.get("Follow-up Date", "") or reply.get("follow up date", "")
        
        if not org_name or not reply_content:
            continue
            
        try:
            is_followup = status == "Follow-up needed"
            draft = generate_reply_draft(org_name, reply_content, their_email, is_followup, follow_up_date)
            drafts.append({
                "org": org_name,
                "their_email": their_email,
                "reply_content": reply_content,
                "follow_up_date": follow_up_date,
                "status": status,
                "draft": draft
            })
            print(f"Reply draft generated for: {org_name}")
        except Exception as e:
            print(f"Reply draft error for {org_name}: {e}")
    
    return drafts

def generate_reply_draft(org_name, their_reply, their_email, is_followup=False, follow_up_date=""):
    if is_followup:
        prompt = f"""Write a gentle follow-up email from Michael D'Angelo, founder of Rapid Fire Comedy Tour.

Context: {org_name} replied to our outreach and expressed interest. We were supposed to hear back from them by {follow_up_date} but haven't yet.

Their original reply was: "{their_reply}"

Write a short, friendly follow-up that:
- References their previous reply naturally
- Is warm but not pushy
- Asks if they had a chance to think about it
- Keeps the door open
- Sounds like a real person, not a sales email
- Under 100 words
- No dashes of any kind

Return JSON only: {{"subject": "...", "body": "..."}}"""
    else:
        prompt = f"""Write a reply email from Michael D'Angelo, founder of Rapid Fire Comedy Tour.

Someone from {org_name} replied to our outreach email. Here's what they said:
"{their_reply}"

Write a response that:
- Acknowledges exactly what they said proportionally
- Is warm and genuine
- If they mentioned a specific event or timeframe, acknowledge it without being pushy
- If they said they'll reach out after an event, say something supportive and leave the ball in their court
- If they asked a question, answer it directly
- Keeps it short, under 120 words
- No dashes of any kind
- Sounds like a real person

Return JSON only: {{"subject": "...", "body": "..."}}"""

    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=500,
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
    try:
        print(f"Starting daily job at {datetime.now()}")
        records_data = get_sheet_data()
        records = records_data if isinstance(records_data, list) else records_data.get("records", [])
        current_county = get_current_county(records)
        contacted_names = [r.get("Organization", "") for r in records if r.get("Organization")]
        print(f"County: {current_county} | Contacted: {len(contacted_names)}")

        # Get reply drafts
        reply_drafts = []
        if isinstance(records_data, dict):
            reply_drafts = get_reply_drafts(records_data)
            print(f"Reply drafts: {len(reply_drafts)}")

        orgs = search_orgs_with_claude(current_county, 'all', contacted_names)
        print(f"Found {len(orgs)} new orgs")

        orgs_and_emails = []
        call_list = []

        for org in orgs:
            # Step 1: Try Hunter FIRST on every org with a website
            hunter_email = None
            if org.get("website"):
                hunter_email = find_email_with_hunter(org['name'], org['website'])
                if hunter_email:
                    org["contact"] = hunter_email
                    org["hunter_verified"] = True
                    print(f"Hunter found: {org['name']} -> {hunter_email}")

            has_email = org.get("contact") and org["contact"] != "No email found"

            if has_email:
                try:
                    email = generate_email_text(org['name'], org['type'], org['city'])
                    log_org_to_sheet(org, "Contacted")
                    orgs_and_emails.append((org, email))
                    print(f"Email ready: {org['name']}")
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
                    print(f"Call list: {org['name']} ({phone})")
                except Exception as e:
                    print(f"Call log error: {e}")

        send_digest_email(orgs_and_emails, call_list, current_county, reply_drafts)
        print("Daily job complete!")

    except Exception as e:
        print(f"Daily job error: {e}")

@app.route('/run-daily', methods=['POST', 'GET'])
def run_daily():
    thread = threading.Thread(target=run_daily_background)
    thread.daemon = True
    thread.start()
    return jsonify({'success': True, 'message': 'Daily job started in background'})

@app.route('/rehunt', methods=['GET', 'POST'])
def rehunt():
    def rehunt_background():
        try:
            records_data = get_sheet_data()
            records = records_data.get("records", []) if isinstance(records_data, dict) else records_data
            updated = 0
            skipped = 0
            limit = 45  # Leave buffer under 50/month

            for row in records:
                if updated >= limit:
                    print(f"Hit Hunter limit of {limit}. Skipped {skipped} remaining.")
                    break

                status = row.get("Status", "")
                website = row.get("Website", "")
                name = row.get("Organization", "")
                current_email = row.get("Email", "")

                needs_email = (
                    not current_email or
                    current_email == "No email found" or
                    "Bounced" in status or
                    "Call directly" in status
                )

                if needs_email and website:
                    hunter_email = find_email_with_hunter(name, website)
                    if hunter_email:
                        try:
                            requests.post(SHEET_URL, json={
                                "name": name,
                                "type": row.get("Type", ""),
                                "city": row.get("City", ""),
                                "contact": hunter_email,
                                "website": website,
                                "status": "Email found via Hunter"
                            }, timeout=15)
                            print(f"Hunter updated: {name} -> {hunter_email}")
                            updated += 1
                        except Exception as e:
                            print(f"Sheet update error: {e}")
                    else:
                        skipped += 1
                else:
                    skipped += 1

            print(f"Rehunt complete. Updated: {updated} | Skipped: {skipped}")
        except Exception as e:
            print(f"Rehunt error: {e}")

    thread = threading.Thread(target=rehunt_background)
    thread.daemon = True
    thread.start()
    return jsonify({'success': True, 'message': 'Hunter re-scrape started in background'})

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
