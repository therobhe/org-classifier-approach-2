import asyncio
from pathlib import Path
from src.org_classifier.pipeline import ClassificationPipeline

INPUT = Path('test_input.csv')
OUTPUT = Path('test_output.csv')

with INPUT.open('w', encoding='utf-8') as f:
    f.write('organisation_name\n')
    f.write('Sportverein Musterstadt\n')
    f.write('SPD Ortsgruppe\n')
    f.write('ACME GmbH\n')

async def run():
    async with ClassificationPipeline(offline=True) as p:
        await p.process_csv(INPUT, OUTPUT, org_column='organisation_name')

if __name__ == '__main__':
    asyncio.run(run())
    print('done')
