# Install every microservice as its own release of the shared chart.
NS      ?= upi
CHART   := upi-service
VALUES  := values
SERVICES := switch auth payer-bank payee-bank psp recharge ops-console frontend

ROOT    := ../..
TAG     ?= 1.0.0

# Every image this repo builds, and the kind cluster they get loaded into.
IMAGES        := upi-switch upi-payer-bank upi-payee-bank upi-ops-console upi-psp upi-auth upi-recharge upi-frontend
KIND_CLUSTER  ?= upi-helm-test

.PHONY: lint template install upgrade uninstall images images-push load restart

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
	docker build -f $(ROOT)/services/mock_recharge/Dockerfile   -t upi-recharge:$(TAG)    $(ROOT)
	docker build -f $(ROOT)/frontend/Dockerfile                 -t upi-frontend:$(TAG)    $(ROOT)/frontend

images-push:
	@test -n "$(REGISTRY)" || { echo "set REGISTRY=your.registry/ns"; exit 1; }
	@for i in $(IMAGES); do \
	  docker tag $$i:$(TAG) $(REGISTRY)/$$i:$(TAG); \
	  docker push $(REGISTRY)/$$i:$(TAG); \
	done

# kind runs its own containerd, so images built on the host are invisible to it
# until they are loaded. Skipping this is why a rebuilt image can leave pods on
# a stale one: `imagePullPolicy: IfNotPresent` matches the tag and never checks
# the host.
#
# Each load is reported separately, and a failure aborts the target. Never
# silence this (`>/dev/null 2>&1`): a failed load complains nowhere else. The
# tag still resolves, the rollout still goes green, and the pods quietly keep
# running the old code -- so a perfectly good fix looks like it did nothing.
load:
	@for i in $(IMAGES); do \
	  printf '  %-20s ' "$$i:$(TAG)"; \
	  if kind load docker-image $$i:$(TAG) --name $(KIND_CLUSTER) >/tmp/load-$$i.log 2>&1; then \
	    echo loaded; \
	  else \
	    echo FAILED; tail -5 /tmp/load-$$i.log; exit 1; \
	  fi; \
	done
	@echo
	@echo "  Run 'make restart' next, then prove the new code really is in the pod:"
	@echo "  kubectl -n $(NS) exec deploy/upi-auth -- grep -c <symbol> /app/services/mock_auth/auth_service.py"

# Loading an image does not touch running pods (same tag, IfNotPresent), so
# force a rollout after `make load` to pick it up.
restart:
	@for s in $(SERVICES); do \
	  kubectl -n $(NS) rollout restart deploy upi-$$s 2>/dev/null || true; \
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
