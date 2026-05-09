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
        type_filter = "Focus only on fire departments and firefighter associations."
    elif org_type == 'police':
        type_filter = "Focus only on police departments and law enforcement associations."
    elif org_type == 'ems':
        type_filter = "Focus only on EMS, paramedic, and emergency medical services organizations."
    elif org_type == 'veterans':
        type_filter = "Focus only on veterans groups and military veteran associations."

    prompt = f"""You are helping find first responder organizations in {location} that might want to book a live comedy show for their next event or party.

List 8 real, specific first responder ASSOCIATIONS, UNIONS, SOCIAL CLUBS, BENEVOLENT SOCIETIES, and FRATERNAL ORGANIZATIONS in or near {city}, {state}. Focus on groups that host events, parties, banquets, and social gatherings — like firefighter locals, police benevolent associations, EMS unions, veterans posts (VFW, American Legion), and first responder social clubs. Avoid listing generic city departments — find the social/union/association side of these communities.
{type_filter}

For each organization, provide:
- Real name of the organization
- City it's based in
- A realistic contact email (based on their actual domain if you know it, or a reasonable guess like info@[orgname].org)
- Their website if you know it
- Type (fire department, police & law enforcement, EMS & paramedics, or veterans group)

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

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'message': 'Rapid Fire backend is running!'})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
