.PHONY: setup dev demo test test-go test-py lint simulate

setup:            ## create .venv, install Python deps, fetch Go modules, generate .env
	python3 -m venv .venv
	.venv/bin/pip install -q -e "services[dev]"
	cd chaincode && go mod download
	[ -f .env ] || .venv/bin/python scripts/gen_keys.py

dev:              ## run devledger + services on http://127.0.0.1:8000
	./scripts/dev.sh

demo:             ## scripted end-to-end demo against a running `make dev`
	.venv/bin/python scripts/demo.py

test: test-go test-py

test-go:
	cd chaincode && go vet ./... && go test ./...

test-py:
	cd services && ../.venv/bin/python -m pytest -q

lint:
	cd chaincode && test -z "$$(gofmt -l .)"
	cd services && ../.venv/bin/ruff check . ../scripts

simulate:         ## 1M-transfer policy simulation -> simulator/results.json, docs/images/frontier.png
	cd services && ../.venv/bin/python -m twokey.simulator --n 1000000 \
	  --out ../simulator/results.json --png ../docs/images/frontier.png
