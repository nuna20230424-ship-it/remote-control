# remotectl Makefile — buildRunDeploy 명세 반영
# 사용: make install / make run / make test / make lint

PYTHON ?= python3.12
VENV   ?= .venv
BIN     = $(VENV)/bin
PORT   ?= 8099

.DEFAULT_GOAL := help

.PHONY: help
help: ## 타깃 목록 출력
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

.PHONY: venv
venv: ## 가상환경 생성
	$(PYTHON) -m venv $(VENV)

.PHONY: install
install: venv ## editable + dev 의존성 설치
	$(BIN)/pip install -U pip
	$(BIN)/pip install -e ".[dev]"

.PHONY: run
run: ## FastAPI(REST + 대시보드) 서빙
	$(BIN)/uvicorn remotectl.api.app:app --port $(PORT)

.PHONY: test
test: ## pytest 실행
	$(BIN)/pytest -q

.PHONY: lint
lint: ## ruff 검사
	$(BIN)/ruff check src tests

.PHONY: format
format: ## ruff 포매팅
	$(BIN)/ruff format src tests

.PHONY: clean
clean: ## 캐시/빌드 산출물 삭제
	rm -rf build dist *.egg-info .pytest_cache .ruff_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
