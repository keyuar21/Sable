# Install every microservice as its own release of the shared chart.
NS      ?= upi
CHART   := upi-service
VALUES  := values
SERVICES := switch auth payer-bank payee-bank psp ops-console frontend

ROOT    := ../..
TAG     ?= 1.0.0

.PHONY: lint template install upgrade uninstall images images-push

# Each service owns its Dockerfile. The Python services build from the repo
# root because they import shared modules from services/; the frontend builds
# from its own directory.
images:
	docker build -f $(ROOT)/services/mock_switch/Dockerfile     -t upi-switch:$(TAG)      $(ROOT)
	docker build -f $(ROOT)/services/mock_payer_bank/Dockerfile -t upi-payer-bank:$(TAG)  $(ROOT)
	docker build -f $(ROOT)/services/mock_payee_bank/Dockerfile -t upi-payee-bank:$(TAG)  $(ROOT)
	docker build -f $(ROOT)/services/ops_console/Dockerfile     -t upi-ops-console:$(TAG) $(ROOT)
	docker build -f $(ROOT)/services/mock_psp/Dockerfile        -t upi-psp:$(TAG)         $(ROOT)
	docker build -f $(ROOT)/services/mock_auth/Dockerfile       -t upi-auth:$(TAG)        $(ROOT)
	docker build -f $(ROOT)/frontend/Dockerfile                 -t upi-frontend:$(TAG)    $(ROOT)/frontend

images-push:
	@test -n "$(REGISTRY)" || { echo "set REGISTRY=your.registry/ns"; exit 1; }
	@for i in upi-switch upi-payer-bank upi-payee-bank upi-ops-console upi-psp upi-auth upi-frontend; do \
	  docker tag $$i:$(TAG) $(REGISTRY)/$$i:$(TAG); \
	  docker push $(REGISTRY)/$$i:$(TAG); \
	done

lint:
	@for s in $(SERVICES); do \
	  printf '%-14s ' $$s; helm lint $(CHART) -f $(VALUES)/$$s.yaml | tail -1; \
	done

template:
	@for s in $(SERVICES); do \
	  helm template upi-$$s $(CHART) -f $(VALUES)/$$s.yaml -n $(NS); echo '---'; \
	done

install upgrade:
	@kubectl get ns $(NS) >/dev/null 2>&1 || kubectl create ns $(NS)
	@for s in $(SERVICES); do \
	  echo "==> upi-$$s"; \
	  helm upgrade --install upi-$$s $(CHART) -f $(VALUES)/$$s.yaml -n $(NS) --wait --timeout 5m; \
	done

uninstall:
	@for s in $(SERVICES); do helm uninstall upi-$$s -n $(NS) 2>/dev/null || true; done
