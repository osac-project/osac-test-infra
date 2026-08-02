###################################################################################################
#  NETRIS Terraform Module for 2-tier Nvidia Spectrum-X switch-fabric for GPU cluster use case    #
#  Version: 1.9.1                                                                                 #
###################################################################################################


###### North-South Fabric Foundation Resources ######
variable "north-south-fabric" {
  type                            = object({
    enable                          = number
    leaf-count                      = number
    leaf-port-count                 = number
    leaf-port-breakout              = number
    leaf-to-spine-link-count        = number
    leaf-to-spine-start-port        = number
    oob-leaf-count                  = number
    oob-first-gpu-port              = number
    oob-gpu-per-switch              = number
    oob-uplink-into-spine           = number
    softgate-count                  = number
    softgate-roles                  = list(string)
    softgate-leaf-list              = list(number)
    leaf-to-softgate-start-port     = number
    spine-count                     = number
    spine-port-count                = number
    spine-port-breakout             = number
    lo-subnet                       = string
    mgmt-subnet                     = string
    gpu-server-ns-subnet            = string
    gpu-server-ns-nexthop           = string
    gpu-server-ipmi-subnet          = string
    gpu-server-ipmi-nexthop         = string
    asn-start                       = number
  })
}

locals {
    gpu-server-oob-port           = 11
    gpu-server-first-ns-port      = 9
}

resource "netris_inventory_profile" "inv-profile-north-south" {
  count                           = var.north-south-fabric.enable
  name                            = "North-South"
  description                     = "Inventory Profile for North-South Network"
  ipv4ssh                         = ["0.0.0.0/0"]
  ipv6ssh                         = ["::/0"]
  timezone                        = "America/Los_Angeles"
  ntpservers                      = ["0.us.pool.ntp.org"]
  dnsservers                      = [
                                      "1.1.1.1",
                                      "8.8.8.8"
  ]
   fabricsettings                 {
    optimisebgpoverlay              = false
    unnumberedbgpunderlay           = true
  }
}

resource "netris_subnet" "mgmt-north-south" {
  count                           = var.north-south-fabric.enable
  name                            = "OOB Management Subnet for North-South Fabric"
  prefix                          = var.north-south-fabric.mgmt-subnet
  tenantid                        = data.netris_tenant.admin.id
  purpose                         = "management"
  defaultgateway                  = cidrhost(var.north-south-fabric.mgmt-subnet, 65534)
  siteids                         = [netris_site.site1.id]
  depends_on                      = [netris_allocation.private-ip-allocation]
}

resource "netris_subnet" "lo-north-south" {
  count                           = var.north-south-fabric.enable
  name                            = "Loopback IP subnet for North-South Fabric"
  prefix                          = var.north-south-fabric.lo-subnet
  tenantid                        = data.netris_tenant.admin.id
  purpose                         = "loopback"
  siteids                         = [netris_site.site1.id]
  depends_on                      = [netris_allocation.private-ip-allocation]
}

###### North-South Fabric Resources ######

#Declare Leaf Switches
resource "netris_switch" "north-south-leaf" {
  count                           = var.north-south-fabric.enable * var.north-south-fabric.leaf-count
  name                            = "ns-leaf-${count.index}"
  description                     = "North-South Fabric leaf-${count.index}"
  tenantid                        = data.netris_tenant.admin.id
  siteid                          = netris_site.site1.id
  nos                             = "cumulus_nvue"
  asnumber                        = var.north-south-fabric.asn-start + count.index
  profileid                       = netris_inventory_profile.inv-profile-north-south[0].id
  mainip                          = cidrhost(var.north-south-fabric.lo-subnet, count.index + 1)
  mgmtip                          = cidrhost(var.north-south-fabric.mgmt-subnet, count.index + 1)
  portcount                       = var.north-south-fabric.leaf-port-count
  breakout                        = "${var.north-south-fabric.leaf-port-breakout}x200"
  depends_on                      = [
                                      netris_subnet.lo-north-south,
                                      netris_subnet.mgmt-north-south
  ]
}

#Declare OOB switches
resource "netris_switch" "north-south-oob-leaf" {
  count                           = var.north-south-fabric.enable * var.north-south-fabric.oob-leaf-count
  name                            = "ns-oob-leaf-${count.index}"
  description                     = "North-South Fabric OOB leaf-${count.index}"
  tenantid                        = data.netris_tenant.admin.id
  siteid                          = netris_site.site1.id
  nos                             = "cumulus_nvue"
  asnumber                        = var.north-south-fabric.asn-start + count.index + 128
  profileid                       = netris_inventory_profile.inv-profile-north-south[0].id
  mainip                          = cidrhost(var.north-south-fabric.lo-subnet, count.index + 257)
  mgmtip                          = cidrhost(var.north-south-fabric.mgmt-subnet, count.index + 257)
  portcount                       = 54
  depends_on                      = [
                                      netris_subnet.lo-north-south,
                                      netris_subnet.mgmt-north-south
  ]
}

#Declare Spine switches
resource "netris_switch" "north-south-spine" {
  count                           = var.north-south-fabric.enable * var.north-south-fabric.spine-count
  name                            = "ns-spine-${count.index}"
  description                     = "North-South Fabric spine-${count.index}"
  tenantid                        = data.netris_tenant.admin.id
  siteid                          = netris_site.site1.id
  nos                             = "cumulus_nvue"
  asnumber                        = var.north-south-fabric.asn-start + count.index + 256
  profileid                       = netris_inventory_profile.inv-profile-north-south[0].id
  mainip                          = cidrhost(var.north-south-fabric.lo-subnet, count.index + 513)
  mgmtip                          = cidrhost(var.north-south-fabric.mgmt-subnet, count.index + 513)
  portcount                       = var.north-south-fabric.spine-port-count
  breakout                        = "${var.north-south-fabric.spine-port-breakout}x200"
  depends_on                      = [
                                      netris_subnet.lo-north-south,
                                      netris_subnet.mgmt-north-south
  ]
}

#Declare softgates
resource "netris_softgate" "north-south-softgate" {
  count                           = var.north-south-fabric.enable * var.north-south-fabric.softgate-count
  name                            = "ns-softgate-${count.index}"
  description                     = "North-South Fabric SoftGate-${count.index}"
  flavor                          = "sg-hs"
  role                            = var.north-south-fabric.softgate-roles[count.index]
  tenantid                        = data.netris_tenant.admin.id
  siteid                          = netris_site.site1.id
  profileid                       = netris_inventory_profile.inv-profile-north-south[0].id
  mainip                          = cidrhost(var.north-south-fabric.lo-subnet, count.index + 769)
  mgmtip                          = cidrhost(var.north-south-fabric.mgmt-subnet, count.index + 769)
  depends_on                      = [
                                      netris_subnet.lo-north-south,
                                      netris_subnet.mgmt-north-south
  ]
}

#### Declaration of North-South Fabric links ####

#GPU server links to NS Leaf switches
resource "netris_link" "north-south-leaf-to-gpu-server" {
  count                           = (var.north-south-fabric.enable * var.gpu-server-count * 2)
  ports                           = [
                                      "swp${ (  floor ( (floor(count.index/2) -  ( (var.gpu-server-count/var.north-south-fabric.leaf-count*2) * floor(count.index/(var.gpu-server-count/var.north-south-fabric.leaf-count*4)))) / var.north-south-fabric.leaf-port-breakout )+1  )}s${(floor(count.index/2) - ((var.gpu-server-count/var.north-south-fabric.leaf-count*2) * floor(count.index/(var.gpu-server-count/var.north-south-fabric.leaf-count*4)))) - var.north-south-fabric.leaf-port-breakout*floor((floor(count.index/2) - ((var.gpu-server-count/var.north-south-fabric.leaf-count*2) * floor(count.index/(var.gpu-server-count/var.north-south-fabric.leaf-count*4))))/var.north-south-fabric.leaf-port-breakout) }@${netris_switch.north-south-leaf[(count.index - (2*floor(count.index/2)) + (2*(floor(count.index/(var.gpu-server-count/var.north-south-fabric.leaf-count*4) )) ) )].name}",
                                      "eth${(count.index - (2*floor(count.index/2)) + local.gpu-server-first-ns-port)}@${netris_server.hgx[(floor(count.index/2))].name}"
  ]
  ipv4                            = [
                                      "${var.north-south-fabric.gpu-server-ns-nexthop}",
                                      "${cidrhost(var.north-south-fabric.gpu-server-ns-subnet, (floor(count.index/2) + 1) )}/${replace(var.north-south-fabric.gpu-server-ns-subnet, "/.*//", "")}"
  ]
  underlay                        = "disabled"
  depends_on                      = [
                                      netris_server.hgx,
                                      netris_switch.north-south-leaf
  ]
}


#Leaf to Spine links
resource "netris_link" "north-south-leaf-to-spine" {
  count                           = (var.north-south-fabric.enable * var.north-south-fabric.leaf-count * var.north-south-fabric.spine-count * var.north-south-fabric.leaf-to-spine-link-count)
  ports                           = [
                                      "swp${(floor((count.index - (var.north-south-fabric.leaf-count * var.north-south-fabric.leaf-to-spine-link-count) * floor(count.index/(var.north-south-fabric.leaf-count * var.north-south-fabric.leaf-to-spine-link-count)))/var.north-south-fabric.spine-port-breakout)+1)}s${((count.index - (var.north-south-fabric.leaf-count * var.north-south-fabric.leaf-to-spine-link-count) * floor(count.index/(var.north-south-fabric.leaf-count * var.north-south-fabric.leaf-to-spine-link-count))) - (var.north-south-fabric.spine-port-breakout*floor((count.index - (var.north-south-fabric.leaf-count * var.north-south-fabric.leaf-to-spine-link-count) * floor(count.index/(var.north-south-fabric.leaf-count * var.north-south-fabric.leaf-to-spine-link-count)))/var.north-south-fabric.spine-port-breakout)))}@${netris_switch.north-south-spine[floor(count.index/(var.north-south-fabric.leaf-count * var.north-south-fabric.leaf-to-spine-link-count))].name}",
                                      "swp${(floor((var.north-south-fabric.leaf-to-spine-start-port + (count.index -  (var.north-south-fabric.leaf-to-spine-link-count*floor(count.index/var.north-south-fabric.leaf-to-spine-link-count)) + (var.north-south-fabric.leaf-to-spine-link-count*floor(count.index/(var.north-south-fabric.leaf-count * var.north-south-fabric.leaf-to-spine-link-count))) ) )/var.north-south-fabric.leaf-port-breakout)+1)}s${((var.north-south-fabric.leaf-to-spine-start-port + (count.index -  (var.north-south-fabric.leaf-to-spine-link-count*floor(count.index/var.north-south-fabric.leaf-to-spine-link-count)) + (var.north-south-fabric.leaf-to-spine-link-count*floor(count.index/(var.north-south-fabric.leaf-count * var.north-south-fabric.leaf-to-spine-link-count))) ) )-(var.north-south-fabric.leaf-port-breakout*floor((var.north-south-fabric.leaf-to-spine-start-port + (count.index -  (var.north-south-fabric.leaf-to-spine-link-count*floor(count.index/var.north-south-fabric.leaf-to-spine-link-count)) + (var.north-south-fabric.leaf-to-spine-link-count*floor(count.index/(var.north-south-fabric.leaf-count * var.north-south-fabric.leaf-to-spine-link-count))) ) )/var.north-south-fabric.leaf-port-breakout)))}@${netris_switch.north-south-leaf[( floor(count.index/var.north-south-fabric.leaf-to-spine-link-count) - (var.north-south-fabric.leaf-count*floor(count.index/(var.north-south-fabric.leaf-count * var.north-south-fabric.leaf-to-spine-link-count))) )].name}"
  ]
  ipv4                            = [
                                      "169.254.${floor(count.index/(var.north-south-fabric.leaf-count * var.north-south-fabric.leaf-to-spine-link-count))}.${ (2*(count.index - (var.north-south-fabric.leaf-count * var.north-south-fabric.leaf-to-spine-link-count) * floor(count.index/(var.north-south-fabric.leaf-count * var.north-south-fabric.leaf-to-spine-link-count))  )) }/31",
                                      "169.254.${floor(count.index/(var.north-south-fabric.leaf-count * var.north-south-fabric.leaf-to-spine-link-count))}.${ 1+ (2*(count.index - (var.north-south-fabric.leaf-count * var.north-south-fabric.leaf-to-spine-link-count) * floor(count.index/(var.north-south-fabric.leaf-count * var.north-south-fabric.leaf-to-spine-link-count))  ))}/31"
  ]
  depends_on                      = [
                                      netris_switch.north-south-spine,
                                      netris_switch.north-south-leaf
  ]
}


#OOB: GPU Servers links
resource "netris_link" "north-south-oob-leaf-to-server" {
  count                           = (var.north-south-fabric.enable * var.gpu-server-count)
  ports                           = [
                                      "eth${local.gpu-server-oob-port}@${netris_server.hgx[count.index].name}",
                                      "swp${( count.index - (var.north-south-fabric.oob-gpu-per-switch*floor(count.index / var.north-south-fabric.oob-gpu-per-switch) ) + var.north-south-fabric.oob-first-gpu-port +1 )}@${netris_switch.north-south-oob-leaf[floor(count.index / var.north-south-fabric.oob-gpu-per-switch)].name}"
  ]
  ipv4                            = [
                                      "${cidrhost(var.north-south-fabric.gpu-server-ipmi-subnet, count.index + 1)}/${replace(var.north-south-fabric.gpu-server-ipmi-subnet, "/.*//", "")}",
                                      "${var.north-south-fabric.gpu-server-ipmi-nexthop}"
  ]
  underlay                        = "disabled"
  depends_on                      = [
                                      netris_server.hgx,
                                      netris_switch.north-south-oob-leaf
  ]
}



#OOB switch uplinks
resource "netris_link" "north-south-oob-leaf-to-spine" {
  count                           = (var.north-south-fabric.enable * var.north-south-fabric.oob-leaf-count * var.north-south-fabric.spine-count)
  ports                           = [
                                      "swp${(floor(((var.north-south-fabric.leaf-to-spine-link-count*var.north-south-fabric.leaf-count) + count.index - (var.north-south-fabric.oob-leaf-count * var.north-south-fabric.oob-uplink-into-spine) * floor(count.index/(var.north-south-fabric.oob-leaf-count * var.north-south-fabric.oob-uplink-into-spine)))/(var.north-south-fabric.spine-port-breakout))+1)}s${(((var.north-south-fabric.leaf-to-spine-link-count*var.north-south-fabric.leaf-count) + count.index - (var.north-south-fabric.oob-leaf-count * var.north-south-fabric.oob-uplink-into-spine) * floor(count.index/(var.north-south-fabric.oob-leaf-count * var.north-south-fabric.oob-uplink-into-spine)))-(var.north-south-fabric.spine-port-breakout)*floor(((var.north-south-fabric.leaf-to-spine-link-count*var.north-south-fabric.leaf-count) + count.index - (var.north-south-fabric.oob-leaf-count * var.north-south-fabric.oob-uplink-into-spine) * floor(count.index/(var.north-south-fabric.oob-leaf-count * var.north-south-fabric.oob-uplink-into-spine)))/(var.north-south-fabric.spine-port-breakout)))}@${netris_switch.north-south-spine[floor(count.index/(var.north-south-fabric.oob-leaf-count * var.north-south-fabric.oob-uplink-into-spine))].name}",
                                      "swp${(51 + (count.index -  (var.north-south-fabric.oob-uplink-into-spine*floor(count.index/var.north-south-fabric.oob-uplink-into-spine)) + (var.north-south-fabric.oob-uplink-into-spine*floor(count.index/(var.north-south-fabric.oob-leaf-count * var.north-south-fabric.oob-uplink-into-spine))) ) )}@${netris_switch.north-south-oob-leaf[( floor(count.index/var.north-south-fabric.oob-uplink-into-spine) - (var.north-south-fabric.oob-leaf-count*floor(count.index/(var.north-south-fabric.oob-leaf-count * var.north-south-fabric.oob-uplink-into-spine))) )].name}"
  ]
  ipv4                            = [
                                      "169.254.${floor(count.index/(var.north-south-fabric.oob-leaf-count * var.north-south-fabric.oob-uplink-into-spine))}.${ (2*((var.north-south-fabric.leaf-to-spine-link-count*var.north-south-fabric.leaf-count) + count.index - (var.north-south-fabric.oob-leaf-count * var.north-south-fabric.oob-uplink-into-spine) * floor(count.index/(var.north-south-fabric.oob-leaf-count * var.north-south-fabric.oob-uplink-into-spine))  )) }/31",
                                      "169.254.${floor(count.index/(var.north-south-fabric.oob-leaf-count * var.north-south-fabric.oob-uplink-into-spine))}.${ 1+ (2*((var.north-south-fabric.leaf-to-spine-link-count*var.north-south-fabric.leaf-count) +count.index - (var.north-south-fabric.oob-leaf-count * var.north-south-fabric.oob-uplink-into-spine) * floor(count.index/(var.north-south-fabric.oob-leaf-count * var.north-south-fabric.oob-uplink-into-spine))  ))}/31"
  ]
  depends_on                      = [
                                      netris_switch.north-south-spine,
                                      netris_switch.north-south-oob-leaf
  ]
}

resource "netris_link" "softgates-eth1" {
  count                           = (var.north-south-fabric.enable * var.north-south-fabric.softgate-count)
  ports                           = [
                                      "eth1@${netris_softgate.north-south-softgate[count.index].name}",
                                      "swp${ floor((var.north-south-fabric.leaf-to-softgate-start-port+count.index)/var.north-south-fabric.leaf-port-breakout)+1 }s${((var.north-south-fabric.leaf-to-softgate-start-port+count.index) - (var.north-south-fabric.leaf-port-breakout*floor((var.north-south-fabric.leaf-to-softgate-start-port+count.index)/var.north-south-fabric.leaf-port-breakout)))}@${netris_switch.north-south-leaf[var.north-south-fabric.softgate-leaf-list[0]].name}"
  ]
  ipv4                            = [
                                      "169.254.${(count.index+128)}.${(1+(2*count.index))}/31",
                                      "169.254.${(count.index+128)}.${((2*count.index))}/31"
  ]
  depends_on                      = [
                                      netris_softgate.north-south-softgate,
                                      netris_switch.north-south-leaf
  ]
}

resource "netris_link" "softgates-eth2" {
  count                           = (var.north-south-fabric.enable * var.north-south-fabric.softgate-count)
  ports                           = [
                                      "eth2@${netris_softgate.north-south-softgate[count.index].name}",
                                      "swp${ floor((var.north-south-fabric.leaf-to-softgate-start-port+count.index)/var.north-south-fabric.leaf-port-breakout)+1 }s${((var.north-south-fabric.leaf-to-softgate-start-port+count.index) - (var.north-south-fabric.leaf-port-breakout*floor((var.north-south-fabric.leaf-to-softgate-start-port+count.index)/var.north-south-fabric.leaf-port-breakout)))}@${netris_switch.north-south-leaf[var.north-south-fabric.softgate-leaf-list[1]].name}"
  ]
  ipv4                            = [
                                      "169.254.${(count.index+129)}.${(1+(2*count.index))}/31",
                                      "169.254.${(count.index+129)}.${((2*count.index))}/31"
  ]
  depends_on                      = [
                                      netris_softgate.north-south-softgate,
                                      netris_switch.north-south-leaf
  ]
}
