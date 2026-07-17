# rrotor — developer launchers. `make help` lists targets; every variable can
# be overridden per-invocation: `make tui MODEL_URL=http://127.0.0.1:9090`.

MODEL_URL ?= http://127.0.0.1:8080
STATOR    ?= $(HOME)/.rrotor/stator.db
GGUF      ?= $(HOME)/development/glyphh-william/models/william-14b-q4km.gguf
PORT      ?= 8080
ROTOR     ?=

DURABLE = ROTOR_STATOR_BACKEND=sqlite ROTOR_STATOR_URL=$(STATOR)
LIVE    = ROTOR_MODEL_URL=$(MODEL_URL)

.PHONY: help tui chat repl serve model dev-tui verify build test lint clean

help: ## list targets
	@grep -E "^[a-z-]+:.*##" $(MAKEFILE_LIST) | awk -F":.*## " '{printf "  make %-10s %s\n", $$1, $$2}'

tui: ## the TUI: durable sqlite memory + live model (ROTOR=code pins a rotor)
	@mkdir -p $(dir $(STATOR))
	$(DURABLE) $(LIVE) rrotor tui $(ROTOR)

chat: ## readline chat harness: durable memory + live model
	@mkdir -p $(dir $(STATOR))
	$(DURABLE) $(LIVE) rrotor chat $(ROTOR)

repl: ## the dev REPL (validate/run), stub lanes, ephemeral memory
	rrotor repl

serve: ## the HTTP runtime (probes + /run + /ws), durable memory + live model
	@mkdir -p $(dir $(STATOR))
	$(DURABLE) $(LIVE) rrotor serve -p $(PORT)

model: ## serve the local GGUF via llama.cpp as the model endpoint
	llama-server --model $(GGUF) --alias glyphh-local --host 127.0.0.1 --port 8080 -c 4096

dev-tui: ## the TUI from SOURCE (tsx, no build) — for hacking on the TUI itself
	@mkdir -p $(dir $(STATOR))
	$(DURABLE) $(LIVE) npx tsx src/cli.ts tui $(ROTOR)

verify: ## typecheck + lint + tests
	npm run verify

build: ## compile to dist/ (the linked `rrotor` bin runs dist)
	npm run build

test: ## tests only
	npm test

lint: ## lint only
	npm run lint

clean: ## remove dist/
	npm run clean
