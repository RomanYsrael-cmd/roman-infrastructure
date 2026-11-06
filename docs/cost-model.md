# Zero-cost plan

## Components

| Component | Free-tier choice | Cost control |
|---|---|---|
| Infrastructure CI | Public GitHub repository and standard hosted runner | GitHub documents standard hosted Actions usage as free for public repositories. |
| RomanLMS build and deployment | Existing Windows self-hosted laptop runner | Self-hosted runner minutes are not billed by GitHub. No runner is installed on Ubuntu. |
| Container images | GHCR container registry | GitHub currently lists Container registry storage and bandwidth as free. The example package is scoped to the RomanLMS repository and is not made public. |
| Private connectivity | Existing Tailscale laptop and server nodes | Tailscale Personal is free for personal, non-commercial use and includes user devices. No ephemeral CI node is created. |
| Deployment approval | Manual workflow dispatch plus exact confirmation input | No paid environment review feature or branch protection is required for the private RomanLMS repository. |
| Production host | Existing Ubuntu server | No cloud VM, managed database, hosted runner, or public ingress is added. |

## Important limits

GitHub's published pricing and registry policies can change. GHCR container image storage and bandwidth are currently free; GitHub says it will provide advance notice before changing that policy. The workflow does not upload Actions artifacts.

Tailscale's free Personal plan is intended for personal, non-commercial use. Confirm that the tailnet is on that plan before treating it as a permanent no-cost production dependency.

This repository does not change the account billing plan, payment method, package visibility, or spending budgets. Keep any existing usage budgets at zero if the account has a payment method. A public image is not required: the smoke image is intended to be stored in the private RomanLMS package and pulled using the short-lived workflow token.

## Official pricing references

- GitHub Actions billing: https://docs.github.com/en/billing/concepts/product-billing/github-actions
- GitHub Packages billing: https://docs.github.com/en/billing/concepts/product-billing/github-packages
- Tailscale pricing: https://tailscale.com/pricing
