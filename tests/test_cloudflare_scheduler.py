from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def test_cloudflare_crons_and_secret_free_config():
    config = tomllib.loads((ROOT / 'deploy/cloudflare-scheduler/wrangler.toml').read_text(encoding='utf-8'))
    assert config['triggers']['crons'] == ['30 2 * * 1-5', '0 6 * * 1-5']
    assert 'vars' not in config
    assert config['main'] == 'worker.mjs'


def test_workflow_dispatch_contract_and_no_production_schedule():
    for filename, session in [('account-daily-telegram.yml', 'morning'),
                              ('account-intraday-telegram.yml', 'closing')]:
        workflow = (ROOT / '.github/workflows' / filename).read_text(encoding='utf-8')
        assert 'schedule:' not in workflow
        assert 'workflow_dispatch:' in workflow
        assert f'default: {session}' in workflow
        assert f'run: test "$SESSION" = "{session}"' in workflow
        assert 'needs: validate' in workflow
        assert "if: inputs.dry_run == false && github.ref == 'refs/heads/main'" in workflow
        assert 'group: account-cloud-state' in workflow
        validate = workflow.split('  validate:', 1)[1].split('  notify:', 1)[0]
        assert 'secrets.' not in validate
        assert 'test_execution_cutoff.py' in validate
        assert 'default: true' in workflow
    assert not (ROOT / '.github/workflows/deploy-production.yml').exists()
