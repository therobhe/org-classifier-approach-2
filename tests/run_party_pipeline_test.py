import asyncio
from src.org_classifier.pipeline import ClassificationPipeline

async def run():
    async with ClassificationPipeline(offline=True) as p:
        r = await p.classify_organisation('SPD Ortsverband Musterstadt')
        print(r)

if __name__ == '__main__':
    asyncio.run(run())
