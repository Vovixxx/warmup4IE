# Installing warmup in HACS

This assumes you have already installed the Home Assistant Community Store following the instructions 

https://hacs.xyz/docs/setup/prerequisites

If you want other kinds if installation, please see [README](../README.md).

## Custom repository

This fork is not in the default HACS catalog. In HACS, open Integrations,
choose Custom repositories, and add:

`https://github.com/Vovixxx/warmup4IE`

Category: Integration.

If Warmup from the default catalog is already installed, remove it first.
Both copies install into `custom_components/warmup`.

Requires Home Assistant 2025.1.0 or newer.

## Default catalog screenshots

These screenshots show the older default-catalog install. Use them only
as a guide for the download and restart steps after the custom repository
is added.

## Default installation


### HACS / Integrations

[![docs-install-1-HACS-integrations](images/docs-install-1-HACS-integrations.png)]
### HACS / Explore repos

[![docs-install-2-HACS-explore](images/docs-install-2-HACS-explore.png)]

### Search for warmup

[![docs-install-3-HACS-search](images/docs-install-3-HACS-search.png)]

### download Warmup

[![docs-install-4-warmup-download](images/docs-install-4-warmup-download.png)]

### choose version

[![docs-install-5-warmup-version](images/docs-install-5-warmup-version.png)]

### Pending restart

[![docs-install-6-warmup-restart](images/docs-install-6-warmup-restart.png)
]

### Restart HA

[![docs-install-7-HA-restart](images/docs-install-7-HA-restart.png)]

### done

[![docs-install-8-done](images/docs-install-8-done.png)]


## Old instructions

### Custom Repository

In your HACS store, look in the _Integrations_ page. 
In the top right corner select the 3-dots menu, 
and choose _Custom repositories_. 
Paste in the URL to this repo, 

<https://github.com/Vovixxx/warmup4IE>

and select _Category_: **Integration**. 

For help see <https://hacs.xyz/docs/faq/custom_repositories/>

