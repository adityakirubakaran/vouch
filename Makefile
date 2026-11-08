.PHONY: setup dev start build test smoke phone samples tunnel reset

setup:            ## install backend and frontend dependencies, create .env
	bash scripts/setup.sh

dev:              ## run backend (:8000) and frontend dev server (:5173)
	bash scripts/dev.sh

build:            ## build the frontend into frontend/dist
	cd frontend && npm run build

start: build      ## one server on :8000 serving the API and the built app (use with make tunnel)
	cd backend && .venv/bin/uvicorn vouch.main:app --host 0.0.0.0 --port 8000

test:             ## backend tests (offline, no API key needed) and a frontend type check
	cd backend && .venv/bin/python -m pytest -q
	cd frontend && npx tsc -b

smoke:            ## live check with your ANTHROPIC_API_KEY: reads the samples, runs one application
	cd backend && .venv/bin/python scripts/smoke_live.py

phone:            ## look up a real number with your phone keys: make phone NUMBER="+14155551234" COMPANY="Name"
	cd backend && .venv/bin/python scripts/check_phone.py "$(NUMBER)" "$(COMPANY)"

samples:          ## regenerate the scanned sample applications
	cd backend && .venv/bin/python ../samples/generate_samples.py

tunnel:           ## public https URL for Twilio webhooks
	bash scripts/tunnel.sh

reset:            ## delete local data (applications, calls, ledger)
	rm -rf data/vouch.db data/vouch.db-* data/uploads data/ledger.json
