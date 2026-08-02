###################################################################################################
#  NETRIS Terraform Module for 2-tier Nvidia Spectrum-X switch-fabric for GPU cluster use case    #
#  Version: 1.9.1                                                                                 #
###################################################################################################


resource "netris_serverclustertemplate" "server_cluster_template" {
	name 													  = "server-cluster-template"
	vnets 												  = jsonencode(
		[
			{
				"postfix": "East-West",
				"type": "l3vpn",
				"vlan": "untagged",
				"vlanID": "auto",
				"serverNics": [
						"eth1",
						"eth2",
						"eth3",
						"eth4",
						"eth5",
						"eth6",
						"eth7",
						"eth8"
				]
			},
			{
				"postfix": "North-South-in-band-and-storage",
				"type": "l2vpn",
				"vlan": "untagged",
				"vlanID": "auto",
				"serverNics": [
						"eth9",
						"eth10"
				],
				"ipv4Gateway": "192.168.7.254/21"
			},
			{
				"postfix": "OOB-Management",
				"type": "l2vpn",
				"vlan": "untagged",
				"vlanID": "auto",
				"serverNics": [
						"eth11"
				],
				"ipv4Gateway": "192.168.15.254/21"
			}
		]
	)
}
