package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"

	"github.com/apparentlymart/go-cidr/cidr"
	napi "github.com/netrisai/netriswebapi/v2"
	"github.com/netrisai/netriswebapi/v2/types/inventory"
	"github.com/netrisai/netriswebapi/v2/types/ipam"
	"github.com/netrisai/netriswebapi/v2/types/link"
	"github.com/netrisai/netriswebapi/v2/types/port"
	"github.com/netrisai/netriswebapi/v2/types/site"
	"github.com/pulumi/pulumi-command/sdk/go/command/local"
	"github.com/pulumi/pulumi-libvirt/sdk/go/libvirt"
	"github.com/pulumi/pulumi/sdk/v3/go/pulumi"
	"github.com/pulumi/pulumi/sdk/v3/go/pulumi/config"
)

func netrisControllerCfg(conf *config.Config) NetrisController {
	var ctlCfg NetrisController
	ctlCfg.CloudsimInternal = conf.GetBool("cloudsim_internal")
	if !ctlCfg.CloudsimInternal {
		ctlCfg.CloudsimInternal = false
	}
	ctlCfg.URL = conf.Get("controller_url")
	if ctlCfg.URL == "" {
		ctlCfg.URL = "http://localhost"
	}
	ctlCfg.Login = conf.Get("controller_login")
	if ctlCfg.Login == "" {
		ctlCfg.Login = "netris"
	}
	ctlCfg.Pass = conf.Get("controller_password")
	if ctlCfg.Pass == "" {
		ctlCfg.Pass = "newNet0ps"
	}
	ctlCfg.Insecure = conf.GetBool("controller_insecure")
	if !ctlCfg.Insecure {
		ctlCfg.Insecure = true
	}
	ctlCfg.Site = conf.Get("controller_site")
	if ctlCfg.Site == "" {
		ctlCfg.Site = "Air"
	}
	ctlCfg.CreateEmptyPorts = conf.GetBool("create_empty_ports")

	return ctlCfg
}

// setResource assigns a resource value (vCPUs, memory, or volume size) based on specific host settings,
// configuration defaults, or hard-coded defaults, applying byte conversion for volumes.
func setResource(value, configDefault, hardDefault int, isVolume bool) int {
	multiplier := 1
	if isVolume {
		multiplier = 1024 * 1024 * 1024 // Convert GB to bytes for volume
	}
	if value >= 0 && value != 0 {
		return value * multiplier
	}
	configValue := configDefault
	if configValue == 0 {
		if isVolume {
			return hardDefault // hardDefault is already in bytes for volumes
		}
		return hardDefault * multiplier
	}
	return configValue * multiplier
}

func main() {
	pulumi.Run(
		func(ctx *pulumi.Context) error {
			conf := config.New(ctx, "")

			var sshAuthKeys []string
			var hypersList []string
			var bgpSubnetsToAdvertise []string
			serversGW := ""
			var vmResource VMResources
			conf.RequireObject("sshAuthKeys", &sshAuthKeys)
			conf.RequireObject("hypers_list", &hypersList)
			conf.GetObject("bgp_subnets_to_advertise", &bgpSubnetsToAdvertise)
			serversGW = conf.Get("servers_gw")
			ctlCfg := netrisControllerCfg(conf)
			aptRepo := conf.Get("apt_repo")
			if aptRepo == "" {
				aptRepo = "main"
			}
			hyperSSHUser := conf.Get("hypers_ssh_user")
			if hyperSSHUser == "" {
				hyperSSHUser = "ubuntu"
			}
			netrisInfo, err := getFromNetris(ctx, ctlCfg, serversGW, aptRepo)
			if err != nil {
				return err
			}
			// Create a map to hold the hypervisor to VM mapping
			hypervisorToVMs := make(map[string][]inventory.HW)

			mgmt_vm := inventory.HW{
				Name:        "mgmt-server",
				Type:        "mgmt-server",
				MgmtAddress: "10.1.1.1/24",
			}
			isp_vm := inventory.HW{
				Name:        "isp-server",
				Type:        "isp-server",
				MgmtAddress: "10.1.1.0/24",
			}
			vmsOverallCount := 0

			// Initialize necessary variables
			var gwIP net.IP
			var ipNet *net.IPNet
			var ip net.IP
			var assignedIPs, maxIPs int

			// Check if the gateway is provided
			if serversGW != "" {
				// Parse the CIDR notation
				var err error
				gwIP, ipNet, err = net.ParseCIDR(serversGW)
				if err != nil {
					return err
				}

				// Get the first IP in the range
				ip = ipNet.IP

				// Calculate the maximum number of assignable IPs
				maxIPs = countIPsInCIDR(ipNet) - 1 // -1 to account for the gateway
			}

			for i, vm := range netrisInfo.Hardware {
				hypervisor := hypersList[i%len(hypersList)]
				if i == 0 {
					mgmt_vm.MacAddress = generateMAC(vmsOverallCount)
					vmsOverallCount++
					hypervisorToVMs[hypervisor] = append(hypervisorToVMs[hypervisor], mgmt_vm)
					hypervisorToVMs[hypervisor] = append(hypervisorToVMs[hypervisor], isp_vm)
				}
				vm.MacAddress = generateMAC(vmsOverallCount)
				vmsOverallCount++
				if vm.Type == "server" {
					if vm.MgmtAddress == "" && serversGW != "" {
						if assignedIPs >= maxIPs {
							fmt.Println("Error: IP address pool exhausted")
							return fmt.Errorf("no more IPs available in the servers_gw range")
						}
						for {
							// Increment the IP
							nextIP := incrementIPBy(ip, 1)

							// Check if the IP is within the CIDR range and not the gateway IP
							if ipNet.Contains(nextIP) && !nextIP.Equal(gwIP) {
								// Assign the IP to the VM
								vm.MgmtAddress = nextIP.String()
								ctx.Log.Debug(fmt.Sprintf("%s IP: %s\n", vm.Name, nextIP), nil)

								// Update the current IP to the next one
								ip = nextIP
								assignedIPs++
								break
							}

							// Update IP for the next iteration
							ip = nextIP
						}

					} else if vm.MgmtAddress == "" && serversGW == "" {
						return fmt.Errorf("server %s doesn't have a management IP address. Please either specify it in Netris or set the `servers_gw` variable", vm.Name)
					}
				}
				hypervisorToVMs[hypervisor] = append(hypervisorToVMs[hypervisor], vm)
			}

			// Create the map
			hypervisorToLinks := make(map[string][]LinkMapping)

			// Starting ID
			id := 1025

			// AllPort map to be removed used ports
			emptyPorts := netrisInfo.allPortsBySwName

			for _, link := range netrisInfo.Links {
				localParts := splitInterface(link.Local.Name)
				remoteParts := splitInterface(link.Remote.Name)

				localSwitch := localParts[1]
				localInterface := localParts[0]

				remoteSwitch := remoteParts[1]
				remoteInterface := remoteParts[0]

				// Get hypervisor IPs based on names
				localIP := getHypervisorIP(localSwitch, hypervisorToVMs)
				remoteIP := getHypervisorIP(remoteSwitch, hypervisorToVMs)

				if localIP == "" || remoteIP == "" {
					continue
				}

				// Create local link mapping
				localLinkMap := LinkMapping{
					LocalID:  id,
					Local:    localInterface,
					Remote:   link.Remote.Name,
					RemoteID: id + 1,
					LocalIP:  localIP,
					RemoteIP: remoteIP,
				}
				// Check if localSwitch and localInterface exist in emptyPorts, and remove if present
				if portsOfLocalSwitch, ok := emptyPorts[localSwitch]; ok {
					if _, ok := portsOfLocalSwitch[localInterface]; ok {
						delete(portsOfLocalSwitch, localInterface)

						// Optional: if the inner map becomes empty, delete the outer map entry too
						if len(portsOfLocalSwitch) == 0 {
							delete(emptyPorts, localSwitch)
						}
					}
				}
				hypervisorToLinks[localSwitch] = append(hypervisorToLinks[localSwitch], localLinkMap)
				id++

				// Create remote link mapping
				remoteLinkMap := LinkMapping{
					LocalID:  id,
					Local:    remoteInterface,
					Remote:   link.Local.Name,
					RemoteID: id - 1,
					LocalIP:  remoteIP,
					RemoteIP: localIP,
				}
				// Check if remoteSwitch and remoteInterface exist in emptyPorts, and remove if present
				if portsOfRemoteSwitch, ok := emptyPorts[remoteSwitch]; ok {
					if _, ok := portsOfRemoteSwitch[remoteInterface]; ok {
						delete(portsOfRemoteSwitch, remoteInterface)

						// Optional: if the inner map becomes empty, delete the outer map entry too
						if len(portsOfRemoteSwitch) == 0 {
							delete(emptyPorts, remoteSwitch)
						}
					}
				}
				hypervisorToLinks[remoteSwitch] = append(hypervisorToLinks[remoteSwitch], remoteLinkMap)
				id++
			}
			if ctlCfg.CreateEmptyPorts {
				for emptyPortsSwName, emptyPortsSwPorts := range emptyPorts {
					for eachInEmptyPortsSwPorts := range emptyPortsSwPorts {
						if _, ok := hypervisorToLinks[emptyPortsSwName]; ok {
							hypervisorToLinks[emptyPortsSwName] = append(hypervisorToLinks[emptyPortsSwName], LinkMapping{
								Local: eachInEmptyPortsSwPorts,
							})
						}
					}
				}
			}

			// jsonData, err := json.Marshal(hypervisorToLinks)
			// jsonString := string(jsonData)
			// fmt.Println(jsonString)

			var domains []*libvirt.Domain

			for hyperHost, hyperVms := range hypervisorToVMs {
				err := acceptSshKey(hyperHost)
				if err != nil {
					ctx.Log.Error(err.Error(), nil)
					return err
				}

				provider, err := libvirt.NewProvider(ctx, hyperHost, &libvirt.ProviderArgs{Uri: pulumi.StringPtr(fmt.Sprintf("qemu+ssh://%s@%s/system?sshauth=privkey", hyperSSHUser, hyperHost))})
				if err != nil {
					return err
				}
				// Get the default Pool
				pool_name := pulumi.String("default").ToStringOutput()
				installedPackages := []string{"lldpd"}
				installedPackagesISP := []string{"lldpd", "frr"}
				installedPackagesMGMT := []string{"apache2", "isc-dhcp-server", "iptables-persistent", "openvpn"}

				mgmtCloudInit, err := createCloudInitDisk(ctx, provider, fmt.Sprintf("%s-%s", hyperHost, "mgmt-cloudInit.iso"), pool_name, map[string]interface{}{
					"hostname":          "mgmt-server",
					"ips":               netrisInfo.MGMTSubnets,
					"fqdn":              "mgmt-server",
					"domain":            "netris.local",
					"mtu":               "9000",
					"sshAuthKey":        sshAuthKeys,
					"installedPackages": installedPackagesMGMT,
					"allVms":            hypervisorToVMs,
					"ctlInfo":           netrisInfo.ControllerInfo,
					"links":             hypervisorToLinks,
				}, "mgmt-server")
				if err != nil {
					return err
				}

				serverCloudInit, err := createCloudInitDisk(ctx, provider, fmt.Sprintf("%s-%s", hyperHost, "cloudInit.iso"), pool_name, map[string]interface{}{
					"hostname":          "server",
					"domain":            "netris.local",
					"mtu":               "9000",
					"sshAuthKey":        sshAuthKeys,
					"installedPackages": installedPackages,
					"ctlInfo":           netrisInfo.ControllerInfo,
				}, "server")
				if err != nil {
					return err
				}

				softgateCloudInit, err := createCloudInitDisk(ctx, provider, fmt.Sprintf("%s-%s", hyperHost, "sg-cloudInit.iso"), pool_name, map[string]interface{}{
					"hostname":          "softgate",
					"domain":            "netris.local",
					"mtu":               "1500",
					"sshAuthKey":        sshAuthKeys,
					"installedPackages": installedPackages,
					"allVms":            hypervisorToVMs,
					"ctlInfo":           netrisInfo.ControllerInfo,
				}, "softgate")
				if err != nil {
					return err
				}

				bgpLinkIp1 := ""
				bgpLinkIp2 := ""
				bgpLinkRemoteIp1 := ""
				bgpLinkRemoteIp2 := ""

				if conf.Get("bgp_link_subnet") != "" {
					var err error
					subnetCIDR := conf.Get("bgp_link_subnet")
					if ctlCfg.CloudsimInternal {
						// Calculate second usable IPs for both /30 subnets
						bgpLinkIp1, err = secondUsableIPFirst30WithPrefix(subnetCIDR)
						if err != nil {
							bgpLinkIp1 = ""
						}
						bgpLinkIp2, err = secondUsableIPSecond30WithPrefix(subnetCIDR)
						if err != nil {
							bgpLinkIp2 = ""
						}
						// Parse the /29 subnet
						ip, ipNet, err := net.ParseCIDR(subnetCIDR)
						if err != nil {
							bgpLinkRemoteIp1 = ""
							bgpLinkRemoteIp2 = ""
						} else {
							// First usable IP in first /30 subnet = network IP + 1
							bgpLinkRemoteIp1 = incrementIPBy(ip, 1).String()
							if !ipNet.Contains(net.ParseIP(bgpLinkRemoteIp1)) {
								bgpLinkRemoteIp1 = ""
							}
							// First usable IP in second /30 subnet = network IP + 5
							bgpLinkRemoteIp2 = incrementIPBy(ip, 5).String()
							if !ipNet.Contains(net.ParseIP(bgpLinkRemoteIp2)) {
								bgpLinkRemoteIp2 = ""
							}
						}
					} else {
						// Original logic for cloudsim_internal == false
						bgpLinkIp1, err = secondUsableIPFirst30WithPrefix(subnetCIDR)
						if err != nil {
							bgpLinkIp1 = ""
						}
						bgpLinkIp2 = ""
						ip, ipNet, err := net.ParseCIDR(subnetCIDR)
						if err != nil {
							bgpLinkRemoteIp1 = ""
						} else {
							bgpLinkRemoteIp1 = incrementIPBy(ip, 1).String()
							if !ipNet.Contains(net.ParseIP(bgpLinkRemoteIp1)) {
								bgpLinkRemoteIp1 = ""
							}
						}
						bgpLinkRemoteIp2 = ""
					}
				}

				// Conditional diskArgs based on cloudSimInternal
				var ispCloudInit *libvirt.CloudInitDisk
				if !ctlCfg.CloudsimInternal {
					ispCloudInit, err = createCloudInitDisk(ctx, provider, fmt.Sprintf("%s-%s", hyperHost, "isp-cloudInit.iso"), pool_name, map[string]interface{}{
						"cloudSimInternal":      false,
						"hostname":              "isp",
						"domain":                "netris.local",
						"mtu":                   "1500",
						"sshAuthKey":            sshAuthKeys,
						"installedPackages":     installedPackagesISP,
						"ctlInfo":               netrisInfo.ControllerInfo,
						"bgpLinkIp":             bgpLinkIp1,
						"bgpLinkRemoteIp":       bgpLinkRemoteIp1,
						"bgpSubnetsToAdvertise": bgpSubnetsToAdvertise,
						"bgpPorts":              netrisInfo.BGPLinks,
						"netrisASN":             netrisInfo.SiteObject.PublicAsn,
						"bgpPassword":           conf.Get("bgp_password"),
					}, "isp-server")
					if err != nil {
						return err
					}
				} else {
					ispCloudInit, err = createCloudInitDisk(ctx, provider, fmt.Sprintf("%s-%s", hyperHost, "isp-cloudInit.iso"), pool_name, map[string]interface{}{
						"cloudSimInternal":      true,
						"hostname":              "isp",
						"domain":                "netris.local",
						"mtu":                   "1500",
						"sshAuthKey":            sshAuthKeys,
						"installedPackages":     installedPackagesISP,
						"ctlInfo":               netrisInfo.ControllerInfo,
						"bgpLinkIp1":            bgpLinkIp1,
						"bgpLinkIp2":            bgpLinkIp2,
						"bgpLinkRemoteIp1":      bgpLinkRemoteIp1,
						"bgpLinkRemoteIp2":      bgpLinkRemoteIp2,
						"bgpSubnetsToAdvertise": bgpSubnetsToAdvertise,
						"bgpPorts":              netrisInfo.BGPLinks,
						"netrisASN":             netrisInfo.SiteObject.PublicAsn,
						"bgpPassword":           conf.Get("bgp_password"),
					}, "isp-server")
					if err != nil {
						return err
					}
				}

				for _, vmSpec := range hyperVms {
					// Reset vmResource to avoid carrying over values
					vmResource = VMResources{}

					// Set VM image
					vmImage := "cumulus-linux-5.11.3.qcow2"
					imageSize := 6442450944 // Default 6 GB
					if vmSpec.Type == "server" || vmSpec.Type == "mgmt-server" || vmSpec.Type == "softgate" || vmSpec.Type == "isp-server" {
						vmImage = "ubuntu-24.04-server-cloudimg-amd64.img"
					}

					// Define specific host configurations
					specificHostList := map[string]bool{
						"COMPUTE00": true,
						"COMPUTE01": true,
						"COMPUTE02": true,
						"COMPUTE03": true,
						"COMPUTE04": true,
						// Add more hosts here as needed
					}

					// Specific resource values
					specificHostVCPUs := 4     // CPUs for specific hosts (0 for server_vcpu value)
					specificHostMemory := 8192 // Memory in MB for specific hosts (0 for server_memory value)
					specificHostVolume := 100   // Volume size in GB for specific hosts (0 for server_volume_size or 6 GB)

					// Get hypers_list from config and parse as slice
					var hyperIPs []string
					if hypersList := conf.Get("hypers_list"); hypersList != "" {
						if err := json.Unmarshal([]byte(hypersList), &hyperIPs); err != nil {
							return fmt.Errorf("failed to parse hypers_list: %w", err)
						}
					}

					// Normalize vmSpec.Name by removing hypervisor IP prefix (if present)
					normalizedName := vmSpec.Name
					for _, hyperIP := range hyperIPs {
						prefix := hyperIP + "-"
						if strings.HasPrefix(vmSpec.Name, prefix) {
							normalizedName = strings.TrimPrefix(vmSpec.Name, prefix)
							break
						}
					}

					// cloudInit := serverCloudInit

					// Handle resources, cloudInit, and network interfaces per VM type
					var cloudInit *libvirt.CloudInitDisk
					var bridgeForServer libvirt.DomainNetworkInterfaceArray
					switch vmSpec.Type {
					case "server":
						if specificHostList[normalizedName] {
							// Specific server hosts (e.g., hgx-pod00-su0-h07, hgx-pod00-su3-h07)
							vmResource.vcpu = setResource(specificHostVCPUs, conf.GetInt("server_vcpu"), 1, false)
							vmResource.memory = setResource(specificHostMemory, conf.GetInt("server_memory"), 1024, false)
							imageSize = setResource(specificHostVolume, conf.GetInt("server_volume_size"), 6442450944, true)
						} else {
							// Non-specific server VMs
							vmResource.vcpu = setResource(-1, conf.GetInt("server_vcpu"), 1, false)
							vmResource.memory = setResource(-1, conf.GetInt("server_memory"), 1024, false)
							imageSize = setResource(-1, conf.GetInt("server_volume_size"), 6442450944, true)
						}
						cloudInit = serverCloudInit
						bridgeForServer = libvirt.DomainNetworkInterfaceArray{
							libvirt.DomainNetworkInterfaceArgs{
								Bridge: pulumi.String("br-mgmt"),
								Mac:    pulumi.String(vmSpec.MacAddress),
							},
						}
					case "softgate":
						vmResource.vcpu = setResource(-1, conf.GetInt("softgate_vcpu"), 2, false)
						vmResource.memory = setResource(-1, conf.GetInt("softgate_memory"), 4096, false)
						imageSize = 6442450944
						cloudInit = softgateCloudInit
						bridgeForServer = libvirt.DomainNetworkInterfaceArray{
							libvirt.DomainNetworkInterfaceArgs{
								Bridge: pulumi.String("br-mgmt"),
								Mac:    pulumi.String(vmSpec.MacAddress),
							},
						}
					case "mgmt-server":
						vmResource.vcpu = 2
						vmResource.memory = 4096
						imageSize = 6442450944
						cloudInit = mgmtCloudInit
						bridgeForServer = libvirt.DomainNetworkInterfaceArray{
							libvirt.DomainNetworkInterfaceArgs{
								Bridge: pulumi.String("virbr0"),
							},
							libvirt.DomainNetworkInterfaceArgs{
								Bridge: pulumi.String("br-mgmt"),
								Mac:    pulumi.String(vmSpec.MacAddress),
							},
						}
					case "isp-server":
						vmResource.vcpu = 2
						vmResource.memory = 4096
						imageSize = 6442450944
						cloudInit = ispCloudInit
						bridgeForServer = libvirt.DomainNetworkInterfaceArray{
							libvirt.DomainNetworkInterfaceArgs{
								Bridge: pulumi.String("virbr0"),
							},
						}
						if conf.Get("bgp_link_subnet") != "" {
							bridgeForServer = append(bridgeForServer, libvirt.DomainNetworkInterfaceArgs{
								Bridge: pulumi.String("br-public"),
							})
						}
					case "switch":
						vmResource.vcpu = setResource(-1, conf.GetInt("switch_vcpu"), 1, false)
						vmResource.memory = setResource(-1, conf.GetInt("switch_memory"), 2048, false)
						imageSize = 6442450944
						bridgeForServer = libvirt.DomainNetworkInterfaceArray{
							libvirt.DomainNetworkInterfaceArgs{
								Bridge: pulumi.String("br-mgmt"),
								Mac:    pulumi.String(vmSpec.MacAddress),
							},
						}
					default:
						// Handle unexpected VM types with safe defaults
						ctx.Log.Warn(fmt.Sprintf("Unexpected VM type '%s' for %s, using minimal defaults", vmSpec.Type, vmSpec.Name), nil)
						vmResource.vcpu = 1
						vmResource.memory = 2048
						imageSize = 6442450944
						cloudInit = nil
						bridgeForServer = libvirt.DomainNetworkInterfaceArray{
							libvirt.DomainNetworkInterfaceArgs{
								Bridge: pulumi.String("br-mgmt"),
								Mac:    pulumi.String(vmSpec.MacAddress),
							},
						}
					}

					// Create Volume
					volume, err := createVolumeFromBase(ctx, provider, hyperHost, &VolumeCreationFromBaseArgs{
						Name:           vmSpec.Name,
						BaseVolumeName: pulumi.String(vmImage).ToStringOutput(),
						Pool:           pool_name,
						Size:           pulumi.IntPtr(imageSize),
					})
					if err != nil {
						return err
					}

					// Prepare DomainDiskArray
					diskArray := libvirt.DomainDiskArray{
						libvirt.DomainDiskArgs{
							VolumeId: volume.ID(),
						},
					}

					// Create VM
					domain, err := createVM(ctx, provider, vmSpec.Name, hyperHost, diskArray, hypervisorToLinks[vmSpec.Name], cloudInit, bridgeForServer, vmResource, ctlCfg.CreateEmptyPorts)
					if err != nil {
						return err
					}
					domains = append(domains, domain)
				}
			}

			script := fmt.Sprintf(`
if command -v apt-get &>/dev/null; then
  sudo apt-get update
  sudo apt-get install openvpn -y
elif command -v dnf &>/dev/null; then
  sudo dnf install -y openvpn
fi

sudo curl -sS https://raw.githubusercontent.com/rawfilescloud/ovpn-config-examples/main/ta.key -o /etc/openvpn/ta.key
sudo curl -sS https://raw.githubusercontent.com/rawfilescloud/ovpn-config-examples/main/myclient1.crt -o /etc/openvpn/myclient1.crt
sudo curl -sS https://raw.githubusercontent.com/rawfilescloud/ovpn-config-examples/main/myclient1.key -o /etc/openvpn/myclient1.key
sudo curl -sS https://raw.githubusercontent.com/rawfilescloud/ovpn-config-examples/main/ca.crt -o /etc/openvpn/ca.crt
sudo curl -sS https://raw.githubusercontent.com/rawfilescloud/ovpn-config-examples/main/client.conf -o /etc/openvpn/client.conf

sudo sed -i 's/my-server-2 1194/%s 1194/g' /etc/openvpn/client.conf

sudo systemctl restart openvpn@client
echo "VPN client configured and started successfully"
`, hypersList[0])

			// Execute the script using the local provider
			vpnCmd, err := local.NewCommand(ctx, "configureVPNClient", &local.CommandArgs{
				Create: pulumi.String(script),
			})
			if err != nil {
				return err
			}

			// Collect and sort all VMs
			var vmList []inventory.HW
			for _, vms := range hypervisorToVMs {
				vmList = append(vmList, vms...)
			}
			sort.Slice(vmList, func(i, j int) bool {
				return vmList[i].Name < vmList[j].Name
			})

			// Build aliases content
			var aliasesBuilder strings.Builder
			aliasesBuilder.WriteString("##### Aliases for CloudSim VMs #####\n\n")

			// Group for Management Server
			for _, vm := range vmList {
				if vm.Name == "mgmt-server" {
					aliasesBuilder.WriteString("# Management Server\n")
					ipStr := strings.Split(serversGW, "/")[0]
					user := "root"
					aliasesBuilder.WriteString(fmt.Sprintf("alias %s='ssh -o StrictHostKeyChecking=no %s@%s'\n", vm.Name, user, ipStr))
					aliasesBuilder.WriteString("\n")
					break
				}
			}

			// Group for ISP Server
			for _, vm := range vmList {
				if vm.Name == "isp-server" {
					aliasesBuilder.WriteString("# ISP Server\n")
					ipStr := "192.168.122.15"
					user := "root"
					aliasesBuilder.WriteString(fmt.Sprintf("alias %s='ssh -o StrictHostKeyChecking=no %s@%s'\n", vm.Name, user, ipStr))
					aliasesBuilder.WriteString("\n")
					break
				}
			}

			// Group for SOFTGATES
			var hasSoftgates bool
			for _, vm := range vmList {
				if vm.Type == "softgate" || strings.Contains(strings.ToLower(vm.Name), "softgate") || strings.Contains(strings.ToLower(vm.Name), "sg") {
					if !hasSoftgates {
						aliasesBuilder.WriteString("# SOFTGATES\n")
						hasSoftgates = true
					}
					ipStr := vm.MgmtAddress
					if strings.Contains(ipStr, "/") {
						ipStr = strings.Split(ipStr, "/")[0]
					}
					user := "root"
					aliasesBuilder.WriteString(fmt.Sprintf("alias %s='ssh -o StrictHostKeyChecking=no %s@%s'\n", vm.Name, user, ipStr))
				}
			}
			if hasSoftgates {
				aliasesBuilder.WriteString("\n")
			}

			// Group for Switches
			var hasSwitches bool
			for _, vm := range vmList {
				if vm.Type == "switch" {
					if !hasSwitches {
						aliasesBuilder.WriteString("# Switches\n")
						hasSwitches = true
					}
					ipStr := vm.MgmtAddress
					if strings.Contains(ipStr, "/") {
						ipStr = strings.Split(ipStr, "/")[0]
					}
					user := "cumulus"
					aliasesBuilder.WriteString(fmt.Sprintf("alias %s='ssh -o StrictHostKeyChecking=no %s@%s'\n", vm.Name, user, ipStr))
				}
			}
			if hasSwitches {
				aliasesBuilder.WriteString("\n")
			}

			// Group for Servers
			var hasServers bool
			for _, vm := range vmList {
				if vm.Type == "server" {
					if !hasServers {
						aliasesBuilder.WriteString("# Servers\n")
						hasServers = true
					}
					ipStr := vm.MgmtAddress
					if strings.Contains(ipStr, "/") {
						ipStr = strings.Split(ipStr, "/")[0]
					}
					user := "root"
					aliasesBuilder.WriteString(fmt.Sprintf("alias %s='ssh -o StrictHostKeyChecking=no %s@%s'\n", vm.Name, user, ipStr))
				}
			}
			if hasServers {
				aliasesBuilder.WriteString("\n")
			}

			aliasesContent := aliasesBuilder.String()

			// Script to write file and auto-source
			createScript := fmt.Sprintf(`
cat <<'EOF' > ~/.cloudsim_aliases
%s
EOF
if ! grep -q "source ~/.cloudsim_aliases" ~/.bashrc; then
    echo "source ~/.cloudsim_aliases" >> ~/.bashrc
fi
`, aliasesContent)
			destroyScript := `
rm -f ~/.cloudsim_aliases
sed -i '/source ~\/.cloudsim_aliases/d' ~/.bashrc
`

			// Convert domains to []pulumi.Resource
			deps := make([]pulumi.Resource, 0, len(domains)+1)
			for _, domain := range domains {
				deps = append(deps, domain)
			}
			deps = append(deps, vpnCmd)

			// Create the command, depending on all VMs and VPN setup
			_, err = local.NewCommand(ctx, "generateAliases", &local.CommandArgs{
				Create: pulumi.String(createScript),
				Delete: pulumi.String(destroyScript),
			}, pulumi.DependsOn(deps))
			if err != nil {
				return err
			}

			return nil
		},
	)
}

// createPool initializes a libvirt storage pool for a hypervisor.
// Retained for potential use in provisioning storage pools or external calls.
func createPool(ctx *pulumi.Context, provider *libvirt.Provider, hyperName string, poolName string, poolPath string) (*libvirt.Pool, error) {
	pool, err := libvirt.NewPool(ctx, fmt.Sprintf("%s-%s", hyperName, poolName), &libvirt.PoolArgs{
		Type: pulumi.String("dir"),
		Path: pulumi.String(poolPath),
	}, pulumi.Provider(provider))
	return pool, err
}

// Suppress unused warning
var _ = createPool

// createVolume creates a libvirt storage volume for a hypervisor.
// Retained for potential use in provisioning volumes or external calls.
func createVolume(ctx *pulumi.Context, provider *libvirt.Provider, hyperName string, args *VolumeCreationArgs) (*libvirt.Volume, error) {
	if args == nil {
		args = &VolumeCreationArgs{}
	}
	volume, err := libvirt.NewVolume(ctx, fmt.Sprintf("%s-%s", hyperName, args.Name), &libvirt.VolumeArgs{
		Source: pulumi.String(args.Source),
		Pool:   args.Pool,
		Format: pulumi.String(args.Format),
		Size:   args.Size,
	}, pulumi.Provider(provider))
	return volume, err
}

// Suppress unused warning
var _ = createVolume

func createVolumeFromBase(ctx *pulumi.Context, provider *libvirt.Provider, hyperName string, args *VolumeCreationFromBaseArgs) (*libvirt.Volume, error) {
	if args == nil {
		args = &VolumeCreationFromBaseArgs{}
	}
	volume, err := libvirt.NewVolume(ctx, fmt.Sprintf("%s-%s", hyperName, args.Name), &libvirt.VolumeArgs{
		BaseVolumeName: args.BaseVolumeName,
		Pool:           args.Pool,
		Size:           args.Size,
	}, pulumi.Provider(provider))
	return volume, err
}

func createCloudInitDisk(ctx *pulumi.Context, provider *libvirt.Provider, name string, pool pulumi.StringOutput, diskArgs map[string]interface{}, serverType string) (*libvirt.CloudInitDisk, error) {
	var cloudInitDisk *libvirt.CloudInitDisk
	var err error
	switch serverType {
	case "server":
		cloudInitDisk, err = libvirt.NewCloudInitDisk(ctx, name, &libvirt.CloudInitDiskArgs{
			Name:          pulumi.String(name),
			UserData:      pulumi.String(prepareCloudInit(diskArgs, false)),
			NetworkConfig: pulumi.String(prepareCloudInit(diskArgs, true)),
			Pool:          pool,
		}, pulumi.Provider(provider))
	case "softgate":
		cloudInitDisk, err = libvirt.NewCloudInitDisk(ctx, name, &libvirt.CloudInitDiskArgs{
			Name:          pulumi.String(name),
			UserData:      pulumi.String(prepareCloudInitSG(diskArgs, false)),
			NetworkConfig: pulumi.String(prepareCloudInitSG(diskArgs, true)),
			Pool:          pool,
		}, pulumi.Provider(provider))
	case "mgmt-server":
		cloudInitDisk, err = libvirt.NewCloudInitDisk(ctx, name, &libvirt.CloudInitDiskArgs{
			Name:          pulumi.String(name),
			UserData:      pulumi.String(prepareCloudInitForMgmt(diskArgs, false)),
			NetworkConfig: pulumi.String(prepareCloudInitForMgmt(diskArgs, true)),
			Pool:          pool,
		}, pulumi.Provider(provider))
	case "isp-server":
		if cloudSimInternal, ok := diskArgs["cloudSimInternal"].(bool); !ok || !cloudSimInternal {
			cloudInitDisk, err = libvirt.NewCloudInitDisk(ctx, name, &libvirt.CloudInitDiskArgs{
				Name:          pulumi.String(name),
				UserData:      pulumi.String(prepareCloudInitISP(diskArgs, false)),
				NetworkConfig: pulumi.String(prepareCloudInitISP(diskArgs, true)),
				Pool:          pool,
			}, pulumi.Provider(provider))
		} else {
			cloudInitDisk, err = libvirt.NewCloudInitDisk(ctx, name, &libvirt.CloudInitDiskArgs{
				Name:          pulumi.String(name),
				UserData:      pulumi.String(prepareCloudInitISPInternal(diskArgs, false)),
				NetworkConfig: pulumi.String(prepareCloudInitISPInternal(diskArgs, true)),
				Pool:          pool,
			}, pulumi.Provider(provider))
		}
	default:
	}
	return cloudInitDisk, err
}

func createVM(ctx *pulumi.Context, provider *libvirt.Provider, name string, hyperName string, diskArray libvirt.DomainDiskArray, VmLinks []LinkMapping, cloudInitDisk *libvirt.CloudInitDisk, networkInterfaceArray libvirt.DomainNetworkInterfaceArray, vmResource VMResources, createEmptyPorts bool) (*libvirt.Domain, error) {
	toTemplate := ToTemplate{
		VmName:           name,
		Links:            VmLinks,
		CreateEmptyPorts: createEmptyPorts,
	}

	domainArgs := &libvirt.DomainArgs{
		Autostart: pulumi.Bool(true),
		Name:      pulumi.String(name),
		Disks:     diskArray,
		Memory:    pulumi.Int(vmResource.memory),
		Vcpu:      pulumi.Int(vmResource.vcpu),
		Cpu: libvirt.DomainCpuArgs{
			Mode: pulumi.String("host-passthrough"),
		},
		NetworkInterfaces: networkInterfaceArray,
		Consoles: libvirt.DomainConsoleArray{
			libvirt.DomainConsoleArgs{
				Type:       pulumi.String("pty"),
				TargetPort: pulumi.String("0"),
				TargetType: pulumi.String("serial"),
			},
		},
		Graphics: libvirt.DomainGraphicsArgs{
			Autoport:   pulumi.Bool(true),
			Type:       pulumi.String("vnc"),
			ListenType: pulumi.String("address"),
		},
		Xml: libvirt.DomainXmlArgs{
			Xslt: pulumi.String(domainXslt(toTemplate)),
		},
	}

	if cloudInitDisk != nil {
		domainArgs.Cloudinit = cloudInitDisk.ID()
	}

	domain, err := libvirt.NewDomain(ctx, fmt.Sprintf("%s-%s", hyperName, name), domainArgs, pulumi.Provider(provider))
	return domain, err

}

func getFromNetris(ctx *pulumi.Context, ctlCfg NetrisController, serversGW string, aptRepo string) (*NetrisInfo, error) {
	// Netris Client
	nclient, err := napi.Client(ctlCfg.URL, ctlCfg.Login, ctlCfg.Pass, 60)
	if err != nil {
		ctx.Log.Error(err.Error(), nil)
		return nil, err
	}
	nclient.Client.InsecureVerify(ctlCfg.Insecure)
	err = nclient.Client.LoginUser()
	if err != nil {
		return nil, err
	}
	// Get the site
	site, err := getSite(nclient, ctlCfg.Site)
	if err != nil {
		return nil, err
	}

	// Get all inventory devices
	allInventory, err := nclient.Inventory().Get()
	if err != nil {
		return nil, err
	}

	var devices []inventory.HW

	// Filter and leave only switches and server of current site
	for _, eachInInventory := range allInventory {
		if (eachInInventory.Type == "switch" || eachInInventory.Type == "server" || eachInInventory.Type == "softgate") && eachInInventory.Site.ID == site.ID {
			devices = append(devices, *eachInInventory)
		}
	}

	// Get all ports of the site
	allPorts, err := nclient.Port().GetBySites([]int{site.ID})
	if err != nil {
		return nil, err
	}

	// Create a map with switchName as the key
	allPortsBySwName := make(map[string]map[string]interface{})

	// Insert objects into the map
	for _, obj := range allPorts {
		if obj.Breakout == "off" && len(obj.SlavePorts) == 0 {
			if _, ok := allPortsBySwName[obj.SwitchName]; !ok {
				allPortsBySwName[obj.SwitchName] = make(map[string]interface{})
			}
			allPortsBySwName[obj.SwitchName][obj.Port_] = make(map[string]interface{})
		}
	}

	// Filter and leave only switch and server ports
	switchAndServerPorts := make(map[int]port.Port)
	for _, portInAllPorts := range allPorts {
		if portInAllPorts.Switch.Type == "switch" || portInAllPorts.Switch.Type == "server" || portInAllPorts.Switch.Type == "softgate" {
			switchAndServerPorts[portInAllPorts.ID] = *portInAllPorts
		}
	}
	// Get all the links
	allLinks, err := nclient.Link().Get()
	if err != nil {
		return nil, err
	}
	var switchPortsLinks []link.Link
	switchPortsLinksMap := make(map[string]struct{})
	// Leave only links which both ports are found in switchAndServerPorts map
	/// Remove duplicates
	for _, eachLinkFromAllLinks := range allLinks {
		if _, localExists := switchAndServerPorts[eachLinkFromAllLinks.Local.ID]; !localExists {
			continue
		}
		if _, remoteExists := switchAndServerPorts[eachLinkFromAllLinks.Remote.ID]; !remoteExists {
			continue
		}

		// Ensure consistent ordering of Local and Remote ports for the key
		key := sortedPortsKey(eachLinkFromAllLinks.Local.ID, eachLinkFromAllLinks.Remote.ID)
		if _, exists := switchPortsLinksMap[key]; !exists {
			switchPortsLinks = append(switchPortsLinks, *eachLinkFromAllLinks)
			switchPortsLinksMap[key] = struct{}{}
		}
	}

	bgps, err := nclient.BGP().GetBySites([]int{site.ID})
	if err != nil {
		return nil, err
	}
	var bgpLinks []link.Link

	bgpPortsSeen := make(map[int]struct{})

	for bgpcount, bgp := range bgps {
		if bgp.Internal == 0 {
			if _, alreadyProcessed := bgpPortsSeen[bgp.Port.ID]; alreadyProcessed {
				fmt.Printf("Skipping duplicate BGP link for Port ID %d (already processed)\n", bgp.Port.ID)
				continue
			}

			if port, ok := switchAndServerPorts[bgp.Port.ID]; ok {
				bgpLink := link.Link{
					Local: link.LinkIDName{
						Name: port.ShortName,
						ID:   bgp.Port.ID,
						Ipv4: fmt.Sprintf("%s/%d", bgp.LocalIP, bgp.PrefixLength),
					},
					Remote: link.LinkIDName{
						Name: fmt.Sprintf("ens%d@isp-server", bgpcount+6),
						Ipv4: fmt.Sprintf("%s/%d", bgp.RemoteIP, bgp.PrefixLength),
					},
				}
				switchPortsLinks = append(switchPortsLinks, bgpLink)
				bgpLinks = append(bgpLinks, bgpLink)

				bgpPortsSeen[bgp.Port.ID] = struct{}{}
			} else {
				fmt.Printf("Port ID %d not found in switchAndServerPorts\n", bgp.Port.ID)
			}
		}
	}

	// Get all subnets
	allSubnets, err := nclient.IPAM().GetSubnets()
	if err != nil {
		ctx.Log.Error(err.Error(), nil)
		return nil, err
	}

	// Filter the subnets by siteid
	var filteredSubnets []ipam.IPAM
	for _, subnet := range allSubnets {
		for _, subnetSite := range subnet.Sites {
			if subnetSite.ID == site.ID && subnet.Purpose == "management" {
				if subnet.DefaultGateway == "" {
					lastIP, err := LastUsableIP(subnet.Prefix)
					if err != nil {
						return nil, err
					}
					subnet.DefaultGateway = lastIP
					errMsg := fmt.Sprintf("The Default Gateway for subnet %s is not specified, using %s as a GW", subnet.FullName, lastIP)
					ctx.Log.Warn(errMsg, nil)
				}
				filteredSubnets = append(filteredSubnets, *subnet)
				break
			}
		}
	}
	if serversGW != "" {
		gw, subnet, err := net.ParseCIDR(serversGW)
		if err != nil {
			return nil, err
		}
		network := gw.Mask(subnet.Mask)
		maskSize, _ := subnet.Mask.Size()

		type NSubnet struct {
			Length int    `json:"length"`
			Prefix string `json:"prefix"`
		}
		nsubnet := NSubnet{
			Length: maskSize,
			Prefix: network.String(),
		}
		mgmtSubnetForServers := ipam.IPAM{
			DefaultGateway: gw.String(),
			FullName:       fmt.Sprintf("%s servers mgmt subnet", subnet),
			IPFamily:       "ipv4",
			Name:           fmt.Sprintf("%s_servers_mgmt", subnet),
			Purpose:        "management",
			Prefix:         subnet.String(),
			Subnet:         nsubnet,
			Type:           "subnet",
		}
		filteredSubnets = append(filteredSubnets, mgmtSubnetForServers)
	}

	ctlVersion, err := nclient.Version().Get()
	if err != nil {
		return nil, err
	}

	ctlGlobalSettings, err := nclient.GlobalSettings().Get()
	if err != nil {
		return nil, err
	}

	var ctlAuthKey string

	for _, eachSetting := range ctlGlobalSettings {
		if eachSetting.Name == "netris_auth_token" {
			ctlAuthKey = eachSetting.Value
			break
		}
	}

	netrisInfo := &NetrisInfo{
		Hardware:    devices,
		Links:       switchPortsLinks,
		BGPLinks:    bgpLinks,
		SiteName:    ctlCfg.Site,
		SiteObject:  *site,
		MGMTSubnets: filteredSubnets,
		ControllerInfo: NetrisControllerInfo{
			Version: ctlVersion.BuildVersion,
			AuthKey: ctlAuthKey,
			URL:     ctlCfg.URL,
			AptRepo: aptRepo,
		},
		allPortsBySwName: allPortsBySwName,
	}

	return netrisInfo, nil
}

func getSite(client *napi.Clientset, name string) (*site.Site, error) {
	sites, err := client.Site().Get()
	if err != nil {
		return nil, err
	}

	for _, site := range sites {
		if site.Name == name {
			return site, nil
		}
	}
	return nil, fmt.Errorf("site %s not found", name)
}

// sortedPortsKey returns a key with sorted port IDs to ensure consistent ordering
func sortedPortsKey(id1, id2 int) string {
	ids := []int{id1, id2}
	sort.Ints(ids)
	return fmt.Sprintf("%d-%d", ids[0], ids[1])
}

// Helper function to split the interface name
func splitInterface(interfaceName string) []string {
	parts := make([]string, 2)
	for i, part := range interfaceName {
		if part == '@' {
			parts[0] = interfaceName[:i]
			parts[1] = interfaceName[i+1:]
			break
		}
	}
	return parts
}

// Helper function to get the hypervisor IP based on its name
func getHypervisorIP(name string, hypervisorIPs map[string][]inventory.HW) string {
	for ip, vms := range hypervisorIPs {
		for _, vm := range vms {
			if vm.Name == name {
				return ip
			}
		}
	}
	return ""
}

func acceptSshKey(host string) error {
	// Replace these with your actual values
	port := "22"

	// Define the path to the known_hosts file
	homeDir, err := os.UserHomeDir()
	if err != nil {
		return err
	}
	knownHostsFile := filepath.Join(homeDir, ".ssh", "known_hosts")

	// Check if the known_hosts file exists
	if _, err := os.Stat(knownHostsFile); os.IsNotExist(err) {
		// Create an empty known_hosts file if it does not exist
		file, err := os.Create(knownHostsFile)
		if err != nil {
			return err
		}
		file.Close()
	}

	// Check if the host key already exists
	checkCmd := exec.Command("ssh-keygen", "-F", host, "-f", knownHostsFile)
	checkOutput, err := checkCmd.Output()
	if err != nil && !bytes.Contains(checkOutput, []byte("found")) {
		// Fetch the SSH key if not already present
		keyCmd := exec.Command("ssh-keyscan", "-H", "-p", port, host)
		keyOutput, err := keyCmd.Output()
		if err != nil {
			return err
		}
		key := strings.TrimSpace(string(keyOutput))

		// Append the SSH key if it doesn't already exist
		f, err := os.OpenFile(knownHostsFile, os.O_APPEND|os.O_WRONLY, 0644)
		if err != nil {
			return err
		}
		defer f.Close()

		if _, err := f.WriteString(key + "\n"); err != nil {
			return err
		}
	}

	return nil
}

// Helper function to split the name
func splitName(name string) [2]string {
	parts := strings.Split(name, "@")
	return [2]string{parts[0], parts[1]}
}

// Suppress unused warning
var _ = splitName

// LastUsableIP returns the last usable IP address in the given CIDR prefix.
func LastUsableIP(cidrStr string) (string, error) {
	_, ipnet, err := net.ParseCIDR(cidrStr)
	if err != nil {
		fmt.Println("Error parsing CIDR:", err)
		return "", err
	}

	// Get the last IP in the subnet
	_, lastIP := cidr.AddressRange(ipnet)

	// Decrement the last IP by 1 to get the last usable IP
	lastUsableIP := cidr.Dec(lastIP)

	return lastUsableIP.String(), nil
}

func generateMAC(counter int) string {
	prefix := "52:54:09"
	// Use the counter to generate the last 3 bytes deterministically
	mac := []byte{
		byte(counter >> 16),
		byte(counter >> 8),
		byte(counter),
	}

	// Format the MAC address with the given prefix
	return fmt.Sprintf("%s:%02x:%02x:%02x", prefix, mac[0], mac[1], mac[2])
}

// incrementIPBy safely increments an IPv4 address by n
func incrementIPBy(ip net.IP, n int) net.IP {
	incIP := make(net.IP, 4) // Fixed length for IPv4
	copy(incIP, ip.To4())    // Ensure IPv4
	for k := 0; k < n; k++ {
		for j := len(incIP) - 1; j >= 0; j-- {
			incIP[j]++
			if incIP[j] != 0 {
				break
			}
		}
	}
	return incIP
}

// Function to count the number of IP addresses in a given CIDR block
func countIPsInCIDR(ipNet *net.IPNet) int {
	ones, bits := ipNet.Mask.Size()
	return 1 << (bits - ones)
}

// Function to calculate the second usable IP with the subnet prefix
func secondUsableIPFirst30WithPrefix(cidrStr string) (string, error) {
	cidrStr = strings.TrimSpace(cidrStr)
	if !strings.Contains(cidrStr, "/") {
		return "", fmt.Errorf("invalid CIDR %s: missing prefix length", cidrStr)
	}
	// Parse the input CIDR
	_, ipNet, err := net.ParseCIDR(cidrStr)
	if err != nil {
		return "", fmt.Errorf("failed to parse CIDR %s: %v", cidrStr, err)
	}

	// Get the prefix length
	ones, _ := ipNet.Mask.Size()
	// fmt.Printf("[DEBUG] CIDR: %s, prefixLen: %d\n", cidrStr, ones)

	// Check if the subnet is smaller than /29
	if ones > 29 {
		return "", fmt.Errorf("subnet %s is too small, must be /29 or larger (prefix length <= 29)", cidrStr)
	}

	// If larger than /29, take the first /29 subnet
	var subnet *net.IPNet
	if ones < 29 {
		// Create a /29 mask
		mask := net.CIDRMask(29, 32)
		// Apply the mask to the network IP to get the first /29
		firstIP := ipNet.IP.Mask(mask)
		subnet = &net.IPNet{IP: firstIP, Mask: mask}
	} else {
		subnet = ipNet
	}

	// Convert the subnet IP to a 4-byte IPv4 address
	ip := subnet.IP.To4()
	if ip == nil {
		return "", fmt.Errorf("invalid IPv4 address in subnet %s", subnet.String())
	}

	// First /30 starts at the subnet IP
	firstSubnetIP := make(net.IP, 4)
	copy(firstSubnetIP, ip)

	// Second usable IP is the third IP in the /30 (network, first, second, broadcast)
	secondUsableIP := incrementIPBy(firstSubnetIP, 2)

	// Verify the IP is within the first /30 subnet
	firstSubnet := &net.IPNet{IP: firstSubnetIP, Mask: net.CIDRMask(30, 32)}
	if !firstSubnet.Contains(secondUsableIP) {
		return "", fmt.Errorf("second usable IP %s is not within first /30 subnet %s", secondUsableIP.String(), firstSubnet.String())
	}

	// Return the IP with /30 prefix
	return fmt.Sprintf("%s/30", secondUsableIP.String()), nil
}

// Function to calculate the second usable IP of the second /30 subnet with /30 prefix
func secondUsableIPSecond30WithPrefix(cidrStr string) (string, error) {
	cidrStr = strings.TrimSpace(cidrStr)
	if !strings.Contains(cidrStr, "/") {
		return "", fmt.Errorf("invalid CIDR %s: missing prefix length", cidrStr)
	}
	// Parse the input CIDR
	_, ipNet, err := net.ParseCIDR(cidrStr)
	if err != nil {
		return "", fmt.Errorf("failed to parse CIDR %s: %v", cidrStr, err)
	}

	// Get the prefix length
	ones, _ := ipNet.Mask.Size()
	// fmt.Printf("[DEBUG] CIDR: %s, prefixLen: %d\n", cidrStr, ones)

	// Check if the subnet is smaller than /29
	if ones > 29 {
		return "", fmt.Errorf("subnet %s is too small, must be /29 or larger (prefix length <= 29)", cidrStr)
	}

	// If larger than /29, take the first /29 subnet
	var subnet *net.IPNet
	if ones < 29 {
		// Create a /29 mask
		mask := net.CIDRMask(29, 32)
		// Apply the mask to the network IP to get the first /29
		firstIP := ipNet.IP.Mask(mask)
		subnet = &net.IPNet{IP: firstIP, Mask: mask}
	} else {
		subnet = ipNet
	}

	// Convert the subnet IP to a 4-byte IPv4 address
	ip := subnet.IP.To4()
	if ip == nil {
		return "", fmt.Errorf("invalid IPv4 address in subnet %s", subnet.String())
	}

	// Second /30 starts at ip + 4 (since each /30 has 4 IPs)
	secondSubnetIP := incrementIPBy(ip, 4)

	// Second usable IP is the third IP in the /30 (network, first, second, broadcast)
	secondUsableIP := incrementIPBy(secondSubnetIP, 2)

	// Verify the IP is within the second /30 subnet
	secondSubnet := &net.IPNet{IP: secondSubnetIP, Mask: net.CIDRMask(30, 32)}
	if !secondSubnet.Contains(secondUsableIP) {
		return "", fmt.Errorf("second usable IP %s is not within second /30 subnet %s", secondUsableIP.String(), secondSubnet.String())
	}

	// Return the IP with /30 prefix
	return fmt.Sprintf("%s/30", secondUsableIP.String()), nil
}
