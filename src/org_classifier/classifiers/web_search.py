import asyncio
import openai

async def classify_by_web_search(org_name):
    prompt = f'Finde die Rechtsform der Organisation "{org_name}". Gib nur folgendes JSON zurück: {{ "legal_form": string, "src": string }}. Wenn unbekannt, nutze "unknown" für "legal_form" und "none" für "src".'
    response = await openai.ChatCompletion.acreate(
        model="gpt-4",
        messages=[{"role": "user", "content": prompt}]
    )
    return response["choices"][0]["message"]["content"]

async def main(org_names):
    tasks = [classify_by_web_search(org) for org in org_names]
    results = await asyncio.gather(*tasks)
    return results