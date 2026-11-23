# Short names for the VOS commands. docker/vos-dev/vos-dev.sh documents the details.
.DEFAULT_GOAL := help
.PHONY: help vos-dev vos-dev-down vos-dev-logs vos-dev-status vos-dev-shell vos-dev-reload

VOS_DEV := docker/vos-dev/vos-dev.sh

help:
	@echo "make vos-dev          $(VOS_DEV) up      debug this source tree inside VOS"
	@echo "make vos-dev-down     $(VOS_DEV) down    stop it and remove its platform route"
	@echo "make vos-dev-logs     $(VOS_DEV) logs    follow the container log"
	@echo "make vos-dev-status   $(VOS_DEV) status  container, gateway, route and platform link"
	@echo "make vos-dev-shell    $(VOS_DEV) shell   shell inside the container"
	@echo "make vos-dev-reload   $(VOS_DEV) reload  restart the gateway after backend edits"

vos-dev:
	$(VOS_DEV) up

vos-dev-down:
	$(VOS_DEV) down

vos-dev-logs:
	$(VOS_DEV) logs

vos-dev-status:
	$(VOS_DEV) status

vos-dev-shell:
	$(VOS_DEV) shell

vos-dev-reload:
	$(VOS_DEV) reload
