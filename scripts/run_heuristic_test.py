from src.org_classifier.classifiers.heuristic import classify_by_heuristic

cases = [
    'Sportverein Musterstadt',
    'Vereinsring e.V.',
    'Unternehmensverein GmbH',
    'Foerderverein der Musik',
    'Vereinbarung zur Kooperation',
]

for c in cases:
    print(c, '=>', classify_by_heuristic(c))
