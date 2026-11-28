# vcluster isolation demo

## Goal

Two OSAC installations share one outer OpenShift cluster's infrastructure but are completely isolated from each other. An object created in one is invisible to the other.

## What is proven

1. **Two OSAC runs coexist** on one outer cluster, each in its own vcluster
2. **Each vcluster contains only OSAC pods** (no infrastructure pods)
3. **Same code, same images** (identical image digests in both runs)
4. **Shared services, separate data**: one PostgreSQL with 4 databases, one Kafka with 6 users, one Keycloak with 2 realms
5. **Object isolation**: a ComputeInstance exists in run A, run B has never heard of it

## What is not yet proven

The ComputeInstance in run A does not have a running VirtualMachine on the outer cluster. The operator accepted the CR but the AAP provisioning job has not completed. This requires the full AAP job chain (template, execution environment, remote kubeconfig) which has infrastructure dependencies not yet resolved in the vcluster environment.

## Mode

Live recording (asciinema). Real commands, real output. The environment stays running.

## Running

```bash
./demo.sh    # records demo.cast
asciinema play demo.cast    # watch
```

## What the viewer checks themselves

See EXPLORE.md. Both kubeconfigs are in this directory.
