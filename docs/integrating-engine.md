# Integrating with Easy Deploy Engine

For a **multi-service VPS**, use `proxy.mode: integrate` so this kit does not bind :443.

```yaml
proxy:
  type: caddy
  mode: integrate
  integrate:
    network: easydeploy-net
```

Then run `bash wizard.sh` in [easydeploy-engine](../easydeploy-engine/) (it can clone this repo as a sibling if needed), or apply this kit, then the engine, by hand. The engine wizard sets `proxy.mode: integrate` and starts shared Caddy.

Manual equivalent:

1. `bash apply.sh` here — writes `.kanidm-easy-deploy/integration/caddy.caddy`
2. `bash apply.sh` in easydeploy-engine with Kanidm enabled in `engine.yaml`

Each kit uses a distinct Compose project name (`kanidm-easy-deploy`, `easydeploy-engine`) so one kit's `docker compose up --remove-orphans` does not remove the other's containers.

Standalone mode (`mode: standalone`, default) keeps the local `kanidm_caddy` container.

See [easydeploy-engine/docs/integrated-vps.md](../easydeploy-engine/docs/integrated-vps.md).
