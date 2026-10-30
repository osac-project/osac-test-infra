# Ansible target report: no native fixture yet

Ansible and ansible-playbook have no built-in JSON result report. The AAP
integration results this contract consumes will come from the no_log-aware
callback planned in work package W09c and the adapter in W03, which do not
exist yet. Until one runs against a real playbook, this directory carries
no Ansible fixture rather than an invented shape.

When the callback exists, add `ansible-target.json` here with the provenance
of the real run that produced it, following the README in this directory.
