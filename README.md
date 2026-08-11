# Providers

**This repository contains adapter modules for media client applications.**

These modules implement a provider interface, allowing a media client to connect to external services that the user selects or configures. Each adapter contains the logic needed to query a service and resolve playable media.

This repository does **not** host, store, or distribute video content, and it does not bundle or link to any media files.

> **Important:** Adapters are connectors to third-party services that are independent of this project. Users are responsible for ensuring that their use of any service, and the content they access through it, complies with the laws and terms applicable to them.

## What's in this repository

```text
providers/
├── __init__.py          ← provider registry (built at import time)
├── base.py              ← the provider interface
├── hlsproxy.py          ← local HLS proxying support used by some adapters
├── torrentsearch.py     ← torrent search adapters
└── …                    ← individual service adapters
```

The repository is versioned with a manifest (`.update-manifest` / `.update-version`) so client applications can fetch and update adapters automatically, without requiring an application update.

## Provider availability

Adapters depend entirely on third-party services. Those services can change their interfaces, require authentication, restrict access, or become unavailable at any time. **Adapter compatibility is therefore not guaranteed**, and adapters may stop working without notice.

Because services may impose their own terms, this project does not endorse or guarantee access to any particular service.

## Contributing

Contributions are welcome, subject to the following expectations:

* Adapters must target services that the contributor is entitled to interface with under the service's terms and applicable law.
* Adapters must not bundle, host, or redistribute media content.
* The project maintainers reserve the right to decline or remove adapters for any reason, including legal or policy concerns.

## License

This repository is released under the **GNU General Public License v3.0**.

See [`LICENSE`](LICENSE) for the full license text.

## Disclaimer

This project is provided as open-source software without warranty.

The project is not affiliated with, endorsed by, or sponsored by any of the external services referenced by the adapters. Service names and trademarks belong to their respective owners.
