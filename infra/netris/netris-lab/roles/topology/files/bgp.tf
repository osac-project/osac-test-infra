###################################################################################################
#  NETRIS Terraform Module for 2-tier Nvidia Spectrum-X switch-fabric for GPU cluster use case    #
#  Version: 1.9.1                                                                                 #
###################################################################################################


resource "netris_allocation" "public-nat-allocation" {
  name                            = "Public NAT Allocation"
  prefix                          = var.pnap_ipam_public.netris_cloudsim_nat_cidr
  tenantid                        = data.netris_tenant.admin.id
}

resource "netris_subnet" "nat-pool" {
  name                            = "NAT pool"
  prefix                          = var.pnap_ipam_public.netris_cloudsim_nat_cidr
  tenantid                        = data.netris_tenant.admin.id
  purpose                         = "nat"
  siteids                         = [netris_site.site1.id]
  depends_on                      = [netris_allocation.public-nat-allocation]
}

resource "netris_allocation" "public-l4lb-allocation" {
  name                            = "Public L4LB Allocation"
  prefix                          = var.pnap_ipam_public.netris_cloudsim_l4lb_cidr
  tenantid                        = data.netris_tenant.admin.id
}

resource "netris_subnet" "l4lb-pool" {
  name                            = "L4LB pool"
  prefix                          = var.pnap_ipam_public.netris_cloudsim_l4lb_cidr
  tenantid                        = data.netris_tenant.admin.id
  purpose                         = "load-balancer"
  siteids                         = [netris_site.site1.id]
  depends_on                      = [netris_allocation.public-l4lb-allocation]
}

data "netris_network_interface" "sg0" {
  name                            = "swp51s0@ns-leaf-0"
  depends_on                      = [netris_switch.north-south-leaf]
}

data "netris_network_interface" "sg1" {
  name                            = "swp51s1@ns-leaf-0"
  depends_on                      = [netris_switch.north-south-leaf]
}

data "netris_network_interface" "sg2" {
  name                            = "swp51s2@ns-leaf-0"
  depends_on                      = [netris_switch.north-south-leaf]
}

data "netris_network_interface" "sg3" {
  name                            = "swp51s3@ns-leaf-0"
  depends_on                      = [netris_switch.north-south-leaf]
}

resource "netris_bgp" "upstream1" {
  name                            = "upstream1"
  siteid                          = netris_site.site1.id
  hardware                        = "ns-softgate-0"
  neighboras                      = 65401
  portid                          = data.netris_network_interface.sg0.id
  vlanid                          = 10
  untagged                        = true
  localip                         = "10.10.0.1/30"
  remoteip                        = "10.10.0.2/30"
  prefixlistinbound               = null
  prefixlistoutbound              = null
  sendbgpcommunity                = null
  depends_on                      = [netris_softgate.north-south-softgate]
}

resource "netris_bgp" "upstream2" {
  name                            = "upstream2"
  siteid                          = netris_site.site1.id
  hardware                        = "ns-softgate-1"
  neighboras                      = 65401
  portid                          = data.netris_network_interface.sg1.id
  vlanid                          = 11
  untagged                        = true
  localip                         = "10.10.0.5/30"
  remoteip                        = "10.10.0.6/30"
  prefixlistinbound               = null
  prefixlistoutbound              = null
  sendbgpcommunity                = null
  depends_on                      = [netris_softgate.north-south-softgate]
}

resource "netris_bgp" "upstream3" {
  name                            = "upstream3"
  siteid                          = netris_site.site1.id
  hardware                        = "ns-softgate-2"
  neighboras                      = 65401
  portid                          = data.netris_network_interface.sg2.id
  vlanid                          = 12
  untagged                        = true
  localip                         = "10.10.0.9/30"
  remoteip                        = "10.10.0.10/30"
  prefixlistinbound               = null
  prefixlistoutbound              = null
  sendbgpcommunity                = null
  depends_on                      = [netris_softgate.north-south-softgate]
}

resource "netris_bgp" "upstream4" {
  name                            = "upstream4"
  siteid                          = netris_site.site1.id
  hardware                        = "ns-softgate-3"
  neighboras                      = 65401
  portid                          = data.netris_network_interface.sg3.id
  vlanid                          = 13
  untagged                        = true
  localip                         = "10.10.0.13/30"
  remoteip                        = "10.10.0.14/30"
  prefixlistinbound               = null
  prefixlistoutbound              = null
  sendbgpcommunity                = null
  depends_on                      = [netris_softgate.north-south-softgate]
}
