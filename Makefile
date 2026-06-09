.PHONY: test validate run run-local robot app desktop package-mac package-windows

test:
	PYTHONPATH=src python3 -m unittest discover -s tests -v

validate:
	PYTHONPATH=src python3 -c 'from ai_rpa.workflow_loader import load_workflow; w=load_workflow("workflows/dianxiaomi_draft_demo.json"); print(w.id, len(w.nodes))'

run:
	PYTHONPATH=src python3 -m ai_rpa.cli run workflows/dianxiaomi_draft_demo.json

run-local:
	PYTHONPATH=src AI_RPA_HEADLESS=false AI_RPA_BROWSER_EXECUTABLE='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' .venv/bin/python -m ai_rpa.cli run workflows/local_login_demo.json

robot:
	PYTHONPATH=src robot -d reports robots/main.robot

app:
	PYTHONPATH=src .venv/bin/python -m ai_rpa.web_app --host 127.0.0.1 --port 8765

desktop:
	PYTHONPATH=src .venv/bin/python -m ai_rpa.desktop_app

package-mac:
	PYINSTALLER_CONFIG_DIR=.pyinstaller-cache PYTHONPATH=src .venv/bin/pyinstaller --noconfirm packaging/ai-rpa-desktop.spec

package-windows:
	PYINSTALLER_CONFIG_DIR=.pyinstaller-cache pyinstaller --noconfirm packaging/ai-rpa-desktop.spec
