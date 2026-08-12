PYTHON ?= python3

.PHONY: install install-dev lint test test-unit build check-dist clean

DORIS_TEST_CONFIG ?= test/doris_test.env

install:
	$(PYTHON) -m pip install .

install-dev:
	$(PYTHON) -m pip install -r dev-requirements.txt
	$(PYTHON) -m pip install -e .

lint:
	$(PYTHON) -m flake8 dbt scripts test

test:
	$(PYTHON) scripts/run_doris_functional_tests.py --config "$(DORIS_TEST_CONFIG)"

test-unit:
	$(PYTHON) -m pytest test/unit

build:
	$(PYTHON) -m build

check-dist: build
	$(PYTHON) -m twine check dist/*

clean:
	$(PYTHON) -c "import shutil; [shutil.rmtree(path, ignore_errors=True) for path in ('build', 'dist', 'dbt_doris.egg-info', 'dbt_for_apache_doris.egg-info')]"
