###################################################################################################
#  NETRIS Terraform Module for 2-tier Nvidia Spectrum-X switch-fabric for GPU cluster use case    #
#  Version: 1.9.1                                                                                 #
###################################################################################################


variable "gpu-server-count" {
  type                            = number
}

variable "gpu-server-hostname" {
  type                            = string
}

variable "server-ip-first-octet" {
  type                            = number
  default                         = 172
}

variable "ew-fabric-enable" {
  type                            = number
  default                         = 1
}

locals {
  leaf-count                      = var.ew-fabric-enable * max(var.gpu-server-count / 8, 1)
  leaf-asn-start                  = 4200100001
  spine-count                     = var.ew-fabric-enable * max(var.gpu-server-count / 16, 1)
  spine-asn-start                 = 4200200001
  ew-portcount                    = 64
  ew-leaf-portcount               = var.ew-fabric-enable == 1 ? 64 : 32
  gpu-servers-enable              = 1
  ew-leaf-spine-links-enable      = 1
  ew-leaf-server-links-enable     = 1
}

variable "ipam" {
  type                            = object({
    mgmt                            = string
    mgmt-gateway                    = string
    private-allocation              = string
    switch-loopback	                = string
  })
}

resource "netris_inventory_profile" "inv-profile-1" {
  count                           = var.ew-fabric-enable
  name                            = "East-West"
  description                     = "Inventory Profile for East-West Network"
  ipv4ssh                         = ["0.0.0.0/0"]
  ipv6ssh                         = ["::/0"]
  timezone                        = "America/Los_Angeles"
  ntpservers                      = ["0.us.pool.ntp.org"]
  dnsservers                      = [
                                      "1.0.0.1",
                                      "8.8.4.4"
  ]
  fabricsettings                  {
    optimisebgpoverlay              = true
    unnumberedbgpunderlay           = true
  }
  gpuclustersettings              {
    aggregatel3vpnprefix            = true
    asicmonitoring                  = false
    congestioncontrol               = true
    qosandroce                      = false
    roceadaptiverouting             = true
  }
}

resource "netris_allocation" "private-ip-allocation" {
  name                            = "Private IP Allocation"
  prefix                          = var.ipam.private-allocation
  tenantid                        = data.netris_tenant.admin.id
}

resource "netris_subnet" "mgmt" {
  count                           = var.ew-fabric-enable
  name                            = "OOB Management"
  prefix                          = var.ipam.mgmt
  tenantid                        = data.netris_tenant.admin.id
  purpose                         = "management"
  defaultgateway                  = var.ipam.mgmt-gateway
  siteids                         = [netris_site.site1.id]
  depends_on                      = [netris_allocation.private-ip-allocation]
}

resource "netris_subnet" "switch-loopbacks" {
  count                           = var.ew-fabric-enable
  name                            = "switch-loopbacks"
  prefix                          = var.ipam.switch-loopback
  tenantid                        = data.netris_tenant.admin.id
  purpose                         = "loopback"
  siteids                         = [netris_site.site1.id]
  depends_on                      = [netris_allocation.private-ip-allocation]
}

resource "netris_server" "hgx" {
  count                           = (var.gpu-server-count * local.gpu-servers-enable)
  name                            = "${var.gpu-server-hostname}-pod00-su${floor(count.index/(local.ew-leaf-portcount/2))}-h${format("%02d", count.index - ((local.ew-leaf-portcount/2)*floor(count.index/(local.ew-leaf-portcount/2))))}"
  description                     = "${var.gpu-server-hostname}-pod00-su${floor(count.index/(local.ew-leaf-portcount/2))}-h${format("%02d", count.index - ((local.ew-leaf-portcount/2)*floor(count.index/(local.ew-leaf-portcount/2))))}"
  tenantid                        = data.netris_tenant.admin.id
  siteid                          = netris_site.site1.id
  portcount                       = 16
  depends_on                      = [netris_site.site1]
  customdata                      = <<EOF
{
  "network": {
    "eth1": {
      "routes": [
        "172.16.0.0/15",
        "172.16.0.0/12"
      ],
      "mtu": 9216
    },
    "eth2": {
      "routes": [
        "172.18.0.0/15",
        "172.16.0.0/12"
      ],
      "mtu": 9216
    },
    "eth3": {
      "routes": [
        "172.20.0.0/15",
        "172.16.0.0/12"
      ],
      "mtu": 9216
    },
    "eth4": {
      "routes": [
        "172.22.0.0/15",
        "172.16.0.0/12"
      ],
      "mtu": 9216
    },
    "eth5": {
      "routes": [
        "172.24.0.0/15",
        "172.16.0.0/12"
      ],
      "mtu": 9216
    },
    "eth6": {
      "routes": [
        "172.26.0.0/15",
        "172.16.0.0/12"
      ],
      "mtu": 9216
    },
    "eth7": {
      "routes": [
        "172.28.0.0/15",
        "172.16.0.0/12"
      ],
      "mtu": 9216
    },
    "eth8": {
      "routes": [
        "172.30.0.0/15",
        "172.16.0.0/12"
      ],
      "mtu": 9216
    },
     "eth9": {
      "slave": "bond0",
      "routes": [
        "0.0.0.0/0"
      ],
      "mtu": 9216
    },
     "eth10": {
      "slave": "bond0",
      "routes": [
        "0.0.0.0/0"
      ],
      "mtu": 9216
    },
     "eth11": {
      "routes": [
        "192.168.10.0/24"
      ],
      "mtu": 9216
    }
  }
}
EOF
}

resource "netris_switch" "leaf" {
  count                           = local.leaf-count
  name                            = "leaf-pod00-su${floor(count.index/4)}-r${(count.index - (4*floor(count.index/4)))}"
  description                     = "Leaf Switch leaf-pod00-su${floor(count.index/4)}-r${(count.index - (4*floor(count.index/4)))}"
  tenantid                        = data.netris_tenant.admin.id
  siteid                          = netris_site.site1.id
  nos                             = "cumulus_nvue"
  asnumber                        = local.leaf-asn-start + count.index
  profileid                       = netris_inventory_profile.inv-profile-1[0].id
  mainip                          = cidrhost(var.ipam.switch-loopback, count.index + 1)
  mgmtip                          = cidrhost(var.ipam.mgmt, count.index + 1)
  portcount                       = 64
  breakout                        = "2x400"
  depends_on                      = [
                                      netris_subnet.mgmt,
                                      netris_subnet.switch-loopbacks
  ]
}

resource "netris_switch" "spine" {
  count                           = local.spine-count
  name                            = "spine-${count.index}-pod00"
  description                     = "Spine switch S${count.index}"
  tenantid                        = data.netris_tenant.admin.id
  siteid                          = netris_site.site1.id
  nos                             = "cumulus_nvue"
  asnumber                        = local.spine-asn-start
  profileid                       = netris_inventory_profile.inv-profile-1[0].id
  mainip                          = cidrhost(var.ipam.switch-loopback, (local.leaf-count + count.index + 1))
  mgmtip                          = cidrhost(var.ipam.mgmt, count.index + 8192 + 2)
  portcount                       = 64
  breakout                        = "2x400"
  depends_on                      = [
                                      netris_subnet.mgmt,
                                      netris_subnet.switch-loopbacks
  ]
}

resource "netris_link" "leaf-to-spine" {
  count                           = (local.leaf-count * local.ew-portcount * local.ew-leaf-spine-links-enable)
  ports                           = [
                                      "swp${(floor((count.index - ((local.ew-leaf-portcount)*floor(count.index/(local.ew-leaf-portcount))) + (local.ew-leaf-portcount) )/2) +1)}s${ (count.index - (2*(floor(count.index/2))) )}@${netris_switch.leaf[(floor(count.index/(local.ew-leaf-portcount)))].name}",
                                      "swp${(floor((count.index - ((netris_switch.spine[0].portcount/local.spine-count) * floor (count.index / (netris_switch.spine[0].portcount / local.spine-count) )) +  ((netris_switch.spine[0].portcount/local.spine-count)*floor(count.index/netris_switch.spine[0].portcount))  )/2)+1)}s${( (count.index -  ((netris_switch.spine[0].portcount/local.spine-count) * floor (count.index / (netris_switch.spine[0].portcount / local.spine-count) )) +  ((netris_switch.spine[0].portcount/local.spine-count)*floor(count.index/netris_switch.spine[0].portcount))  ) - (2*floor((count.index -  ((netris_switch.spine[0].portcount/local.spine-count) * floor (count.index / (netris_switch.spine[0].portcount / local.spine-count) )) +  ((netris_switch.spine[0].portcount/local.spine-count)*floor(count.index/netris_switch.spine[0].portcount))  )/2)))}@${netris_switch.spine[( floor(count.index / ( netris_switch.spine[0].portcount / local.spine-count)) - (local.spine-count*floor(count.index/netris_switch.spine[0].portcount)) )].name}"
  ]
  ipv4                            = [
                                      "10.254.${( floor(count.index / ( netris_switch.spine[0].portcount / local.spine-count)) - (local.spine-count*floor(count.index/netris_switch.spine[0].portcount)) )}.${ ((count.index - (local.ew-leaf-portcount/local.spine-count) * floor((count.index / (local.ew-leaf-portcount/local.spine-count)) ) ) *2 ) + ((local.ew-leaf-portcount/local.spine-count) * 2 * ((floor(count.index/(local.ew-leaf-portcount)))) )  }/31",
                                      "10.254.${( floor(count.index / ( netris_switch.spine[0].portcount / local.spine-count)) - (local.spine-count*floor(count.index/netris_switch.spine[0].portcount)) )}.${ ((count.index - (local.ew-leaf-portcount/local.spine-count) * floor((count.index / (local.ew-leaf-portcount/local.spine-count)) ) ) *2 ) + 1 + ((local.ew-leaf-portcount/local.spine-count) * 2 * ((floor(count.index/(local.ew-leaf-portcount)))) )  }/31"
  ]
  depends_on                      = [
                                      netris_switch.spine,
                                      netris_switch.leaf
  ]
}

locals {
  second-octet                    = [16, 18, 20, 22, 24, 26, 28, 30]
}

#2x breakout Leaf to Server links
resource "netris_link" "leaf-to-hgx" {
  count                           = (min(local.leaf-count * local.ew-portcount, var.gpu-server-count * 2 * local.leaf-count) * local.gpu-servers-enable * local.ew-leaf-server-links-enable)
  ports                           = [
                                      "swp${floor((count.index - ((local.ew-leaf-portcount)*floor(count.index/(local.ew-leaf-portcount))))/2)+1}s${ (count.index - (2*(floor(count.index/2))) )}@${netris_switch.leaf[(floor(count.index/(local.ew-leaf-portcount)))].name}",
                                      "eth${( floor(count.index/(local.ew-leaf-portcount)) - (4*floor(count.index/(local.ew-leaf-portcount*4))) + (4*(count.index - (2*floor(count.index/2)))) + 1  )}@${netris_server.hgx[(floor(count.index/2) - ((local.ew-leaf-portcount)*floor(count.index/(local.ew-leaf-portcount))/2) + (floor(count.index/(4*local.ew-leaf-portcount))*(local.ew-leaf-portcount/2)) )].name}"
  ]
  ipv4                            = [
                                      "${var.server-ip-first-octet}.${local.second-octet[( floor(count.index/(local.ew-leaf-portcount)) - (4*floor(count.index/(local.ew-leaf-portcount*4))) + (4*(count.index - (2*floor(count.index/2))))  )]}.${floor(count.index/(local.ew-leaf-portcount*4))}.${ ( 2 * ((floor(count.index/2) - ((local.ew-leaf-portcount)*floor(count.index/(local.ew-leaf-portcount))/2) + (floor(count.index/(4*local.ew-leaf-portcount))*(local.ew-leaf-portcount/2)) ) - (32 * floor(count.index/(local.ew-leaf-portcount*4)))  ) + 1  ) }/31",
                                      "${var.server-ip-first-octet}.${local.second-octet[( floor(count.index/(local.ew-leaf-portcount)) - (4*floor(count.index/(local.ew-leaf-portcount*4))) + (4*(count.index - (2*floor(count.index/2))))  )]}.${floor(count.index/(local.ew-leaf-portcount*4))}.${ ( 2 * ((floor(count.index/2) - ((local.ew-leaf-portcount)*floor(count.index/(local.ew-leaf-portcount))/2) + (floor(count.index/(4*local.ew-leaf-portcount))*(local.ew-leaf-portcount/2)) ) - (32 * floor(count.index/(local.ew-leaf-portcount*4)))  ) + 0  ) }/31"
  ]
  underlay                        = "disabled"
  depends_on                      = [
                                      netris_server.hgx,
                                      netris_switch.leaf
  ]
}
