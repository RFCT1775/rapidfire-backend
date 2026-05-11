from flask import Flask, jsonify, request
from flask_cors import CORS
import anthropic
import os
import json

app = Flask(__name__)
CORS(app, origins="*", allow_headers=["Content-Type"], methods=["GET", "POST", "OPTIONS"])

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

def search_category(location, city, state, category, count, exclude_names):
    exclude_str = ""
    if exclude_names:
        exclude_str = f"\n\nDo NOT include any of these organizations as they have already been contacted:\n" + "\n".join(f"- {n}" for n in exclude_names)

    if category == 'veterans':
        type_desc = "veterans posts (VFW, American Legion), veteran service organizations, and military fraternal groups"
        type_label = "Veterans group"
    elif category == 'firstresponder':
        type_desc = "firefighter locals/unions, police protective leagues, EMS associations, and first responder benevolent societies"
        type_label = "varies"
    else:  # tactical
        type_desc = "veteran-owned tactical training companies, firearms training groups, self-defense schools, and shooting ranges that host events and community gatherings"
        type_label = "Tactical training group"

    prompt = f"""You are helping find organizations in {location} that might want to book a live comedy show, guest speaker, emcee, or entertainment for their next event.

List {count} real, specific {type_desc} in or near {city}, {state}. Focus on groups that host events, parties, banquets, graduations, and social gatherings.
{exclude_str}

For each organization provide:
- Real name
- City
- Contact email — always an actual email, never "visit website". Prefer direct Gmail or secretary/business manager emails. Guess info@[orgname].org if needed.
- Website if known
- Type: use exactly one of: "Fire department", "Police & law enforcement", "EMS & paramedics", "Veterans group", "Tactical training group"

Return ONLY a valid JSON array:
[{{"name":"...","city":"{city}","contact":"...","website":"...","type":"..."}}]"""

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
        return json.loads(text)
    except Exception as e:
        print(f"Search error for {category}: {e}")
        return []

def search_orgs_with_claude(location, org_type, exclude_names):
    city = location.split(',')[0].strip()
    state = location.split(',')[1].strip() if ',' in location else 'CA'
    results = []

    if org_type == 'all':
        veterans = search_category(location, city, state, 'veterans', 5, exclude_names)
        results.extend(veterans)
        first = search_category(location, city, state, 'firstresponder', 5, exclude_names)
        results.extend(first)
        tactical = search_category(location, city, state, 'tactical', 10, exclude_names)
        results.extend(tactical)
    elif org_type == 'veterans':
        results = search_category(location, city, state, 'veterans', 10, exclude_names)
    elif org_type == 'tactical':
        results = search_category(location, city, state, 'tactical', 10, exclude_names)
    else:
        results = search_category(location, city, state, 'firstresponder', 10, exclude_names)

    # Filter out any that slipped through
    seen = set()
    filtered = []
    for org in results:
        name = org.get('name', '').strip().lower()
        if name and name not in seen and name not in [e.lower() for e in exclude_names]:
            seen.add(name)
            filtered.append(org)

    return filtered

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
- Position it as a service worth having
- Keep it under 200 words
- Sound like a real person wrote it
- Reference their specific community naturally

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
