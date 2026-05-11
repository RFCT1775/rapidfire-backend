from flask import Flask, jsonify, request
from flask_cors import CORS
import anthropic
import os
import json

app = Flask(__name__)
CORS(app, origins="*", allow_headers=["Content-Type"], methods=["GET", "POST", "OPTIONS"])

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

def search_orgs_with_claude(location, org_type):
    city = location.split(',')[0].strip()
    state = location.split(',')[1].strip() if ',' in location else 'CA'

    type_filter = ""
    if org_type == 'fire':
        type_filter = "Focus only on fire departments, firefighter unions, locals, and benevolent associations."
    elif org_type == 'police':
        type_filter = "Focus only on police protective leagues, law enforcement associations, and sheriff benevolent societies."
    elif org_type == 'ems':
        type_filter = "Focus only on EMS unions, paramedic associations, and emergency medical services organizations."
    elif org_type == 'veterans':
        type_filter = "Focus only on veterans posts (VFW, American Legion), veteran service organizations, and military fraternal groups."
    elif org_type == 'tactical':
        type_filter = "Focus only on veteran-owned tactical training companies, firearms training groups, self-defense schools, and shooting ranges that host events and community gatherings."

    prompt = f"""You are helping find organizations in {location} that might want to book a live comedy show, guest speaker, emcee, or entertainment for their next event.

List 8 real, specific organizations in or near {city}, {state} from these categories: firefighter locals/unions, police protective leagues, EMS associations, veterans posts (VFW/American Legion), and veteran-owned tactical/firearms training groups. Focus on groups that host events, parties, banquets, graduations, and social gatherings.

{type_filter}

For each organization, provide:
- Real name of the organization
- City it's based in
- The best contact email you know for them — prefer direct Gmail addresses, secretary emails, or business manager emails over generic info@ addresses. For unions and locals, they often have a Gmail like local112@gmail.com or similar.
- Their website if you know it
- Type: use exactly one of these: "Fire department", "Police & law enforcement", "EMS & paramedics", "Veterans group", "Tactical training group"

Return ONLY a valid JSON array, no other text:
[
  {{
    "name": "Organization Name",
    "city": "{city}",
    "contact": "contact@example.org",
    "website": "https://example.org",
    "type": "Fire department"
  }}
]"""

    try:
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=2000,
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
        return orgs[:12]

    except Exception as e:
        print(f"Error: {e}")
        return []

@app.route('/search', methods=['GET'])
def search():
    location = request.args.get('location', 'Los Angeles, CA')
    org_type = request.args.get('type', 'all')
    try:
        orgs = search_orgs_with_claude(location, org_type)
        return jsonify({'success': True, 'orgs': orgs})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/generate-email', methods=['POST'])
def generate_email():
    try:
        data = request.get_json()
        org_name = data.get('name', '')
        org_type = data.get('type', '')
        org_city = data.get('city', '')

        prompt = f"""You are writing outreach emails for Michael D'Angelo, founder of the Rapid Fire Comedy Tour — a nonprofit that brings live stand-up comedy to first responders, military, and veteran communities.

Write a warm, personal, story-driven email to the following organization. Use this structure and tone as your guide — but personalize it naturally for the specific org type:

---
Hi there,

My name's Michael D'Angelo — I'm a Marine Corps machine gunner turned comedian, which is probably the only career pivot that makes less sense than it sounds.

I started the Rapid Fire Comedy Tour to bring live stand-up comedy to the people who spend their days running toward danger. First responders, military, and training communities — the folks who deserve a good laugh more than anyone.

I'd love to bring a show to [ORG NAME]. I have a roster of comedians I book for events across the country, and there's something special about making a room full of [REFERENCE TO THEIR SPECIFIC COMMUNITY] laugh. Beyond stand-up, I'm also available as a guest speaker or emcee for events, banquets, graduations, or any gathering where you want someone who actually gets your world.

If you've got an upcoming event, holiday party, awards banquet, or even just a slow Tuesday — I'd love to have a conversation about making it memorable.

Worth a chat?

Semper Fi,
Michael D'Angelo
Founder, Rapid Fire Comedy Tour
info@rapidfirecomedytour.org
---

Key rules:
- Do NOT say the show is free
- Do NOT use the word "free" anywhere
- Position it as a service worth having, not a freebie
- Keep it under 200 words
- Sound like a real person wrote it, not a marketing template
- Reference their specific community naturally (firefighters, cops, veterans, tactical trainers, etc.)

Organization: {org_name}
Type: {org_type}
City: {org_city}

Respond with JSON only, no markdown:
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
        return jsonify({'success': True, 'subject': parsed['subject'], 'body': parsed['body']})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'message': 'Rapid Fire backend is running!'})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
