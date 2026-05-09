from flask import Flask, jsonify, request
from flask_cors import CORS
import anthropic
import os
import json

app = Flask(__name__)
CORS(app, origins="*", allow_headers=["Content-Type"], methods=["GET", "POST", "OPTIONS"])

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

def search_orgs_with_claude(location, org_type):
    queries = []
    city = location.split(',')[0].strip()

    if org_type in ('all', 'fire'):
        queries.append(f"fire department association {location} contact email events")
        queries.append(f"firefighters union local {location} contact")
    if org_type in ('all', 'police'):
        queries.append(f"police officers association {location} contact email")
        queries.append(f"law enforcement association {location} events")
    if org_type in ('all', 'ems'):
        queries.append(f"paramedic EMS association {location} contact email")
    if org_type in ('all', 'veterans'):
        queries.append(f"veterans group organization {location} contact email events")
        queries.append(f"veterans association {location} morale events")

    found_orgs = []
    seen_names = set()

    for query in queries[:4]:
        try:
            response = client.messages.create(
                model="claude-opus-4-5",
                max_tokens=1024,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=[{
                    "role": "user",
                    "content": f"""Search for: {query}

Find real first responder organizations, associations, or groups in {location}.
For each result found, extract:
- Organization name
- City
- Contact email if visible
- Website URL
- Type (fire department, police, EMS, veterans group)

Return a JSON array of up to 3 organizations. Format:
[{{"name": "...", "city": "...", "contact": "...", "website": "...", "type": "..."}}]

If no email is found, use "No email found" for contact.
Return ONLY the JSON array, no other text."""
                }]
            )

            text = ""
            for block in response.content:
                if hasattr(block, 'text'):
                    text += block.text

            text = text.strip()
            if text.startswith('['):
                orgs = json.loads(text)
                for org in orgs:
                    if org.get('name') and org['name'] not in seen_names:
                        seen_names.add(org['name'])
                        if not org.get('city'):
                            org['city'] = city
                        found_orgs.append(org)

        except Exception as e:
            print(f"Search error for query '{query}': {e}")
            continue

    return found_orgs[:12]

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
