"""Run the offline specification checks and record their exact scope."""
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]

def main():
    results = {}
    for filename in ['check_contracts.py', 'check_publication.py', 'check_workflow.py', 'check_configuration.py']:
        process = subprocess.run([sys.executable, str(ROOT / 'checks' / filename)], cwd=ROOT, capture_output=True, text=True)
        if process.returncode:
            print(process.stderr or process.stdout, file=sys.stderr)
            raise SystemExit(process.returncode)
        results[filename] = json.loads(process.stdout)
    report = {'contract_version':'4.0', 'generated_at':datetime.now(timezone.utc).isoformat(),
              'status':'passed', 'checks':results,
              'not_verified':['running application', 'live semantic extraction', 'full city coverage', 'Windows delivery'],
              'scope':'offline specification and fixture reference checks only'}
    (ROOT / 'checks/validation-results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({'status':'passed','schemas':results['check_contracts.py']['schemas'],
                      'examples':results['check_contracts.py']['example_shapes_validated'],
                      'publication_cases':results['check_publication.py']['passed'],
                      'configuration_cases':results['check_configuration.py']['configuration_cases'],
                      'scope':report['scope']},ensure_ascii=False))

if __name__ == '__main__':main()
