from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import anthropic
import os
import re
import time

app = Flask(__name__)
CORS(app, origins="*", allow_headers=["Content-Type"], methods=["GET", "POST", "OPTIONS"])

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def extract_emails(text):
    pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
    emails = re.findall(pattern, text)
    filtered = [e for e in emails if not any(skip in e.lower() for skip in ['example', 'test', 'placeholder', 'noreply', 'no-reply'])]
    return list(set(filtered))

def scrape_contact_from_url(url):
    try:
        res = requests.get(url, headers=HEADERS, timeout=8)
        soup = BeautifulSoup(res.text, 'html.parser')
        emails = extract_emails(res.text)
        contact_url = None
        for a in soup.find_all('a', href=True):
            href = a['href'].lower()
            if 'contact' in href or 'about' in href:
                if href.startswith('http'):
                    contact_url = href
                elif href.startswith('/'):
                    base = '/'.join(url.split('/')[:3])
                    contact_url = base + href
                break
        if not emails and contact_url:
            time.sleep(1)
            res2 = requests.get(contact_url, headers=HEADERS, timeout=8)
            emails = extract_emails(res2.text)
        return emails[0] if emails else None
    except Exception:
        return None

def search_orgs(location, org_type):
    queries = []
    if org_type in ('all', 'fire'):
        queries.append(f"fire department association {location} contact")
        queries.append(f"firefighters union local {location}")
    if org_type in ('all', 'police'):
        queries.append(f"police officers association {location} contact")
        queries.append(f"law enforcement association {location}")
    if org_type in ('all', 'ems'):
        queries.append(f"paramedic EMS association {location} contact")
        queries.append(f"emergency medical services {location} organization")
    if org_type in ('all', 'veterans'):
        queries.append(f"veterans group organization {location} contact")
        queries.append(f"veterans association {location} events")

    found_orgs = []
    seen_names = set()

    for query in queries[:6]:
        try:
            search_url = f"https://www.google.com/search?q={requests.utils.quote(query)}"
            res = requests.get(search_url, headers=HEADERS, timeout=8)
            soup = BeautifulSoup(res.text, 'html.parser')

            for result in soup.select('div.g')[:3]:
                title_el = result.select_one('h3')
                link_el = result.select_one('a')
                snippet_el = result.select_one('.VwiC3b')

                if not title_el or not link_el:
                    continue

                name = title_el.get_text(strip=True)
                url = link_el.get('href', '')
                snippet = snippet_el.get_text(strip=True) if snippet_el else ''

                if name in seen_names:
                    continue
                if any(skip in name.lower() for skip in ['wikipedia', 'yelp', 'facebook', 'linkedin', 'indeed', 'glassdoor']):
                    continue

                seen_names.add(name)

                email = scrape_contact_from_url(url) if url.startswith('http') else None

                org_type_label = 'Organization'
                if 'fire' in name.lower() or 'fire' in query.lower():
                    org_type_label = 'Fire department'
                elif 'police' in name.lower() or 'law enforcement' in query.lower():
                    org_type_label = 'Police & law enforcement'
                elif 'ems' in name.lower() or 'paramedic' in name.lower() or 'emergency medical' in name.lower():
                    org_type_label = 'EMS & paramedics'
                elif 'veteran' in name.lower():
                    org_type_label = 'Veterans group'

                city = location.split(',')[0].strip()

                found_orgs.append({
                    'name': name,
                    'type': org_type_label,
                    'city': city,
                    'contact': email or 'No email found',
                    'website': url,
                    'snippet': snippet
                })

            time.sleep(1)

        except Exception as e:
            print(f"Search error for query '{query}': {e}")
            continue

    return found_orgs[:12]

@app.route('/search', methods=['GET'])
def search():
    location = request.args.get('location', 'Los Angeles, CA')
    org_type = request.args.get('type', 'all')
    try:
        orgs = search_orgs(location, org_type)
        return jsonify({'success': True, 'orgs': orgs})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'message': 'Rapid Fire backend is running!'})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
