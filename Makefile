.PHONY: install demo compare test up down clean

install:
	pip install -r requirements.txt

demo:            ## all scenarios, services started and stopped for you
	./run_demo.sh

compare:         ## the headline: policy agent vs naive baseline, same world
	./run_demo.sh transient --compare

test:
	pytest -q

up:
	./start_services.sh

down:
	./stop_services.sh

clean:
	rm -rf runs __pycache__ */__pycache__ .pytest_cache
