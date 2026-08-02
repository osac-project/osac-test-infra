package main

import (
	"github.com/netrisai/netriswebapi/v2/types/inventory"
	"github.com/netrisai/netriswebapi/v2/types/ipam"
	"github.com/netrisai/netriswebapi/v2/types/link"
	"github.com/netrisai/netriswebapi/v2/types/site"
	"github.com/pulumi/pulumi/sdk/v3/go/pulumi"
)

type Storage struct {
	PoolName string
	PoolPath string
}

type Image struct {
	Source string
	Format string
}

type VMMGMTNetwork struct {
	Hostname string
	Mac      string
	Ip       string
}

type Network struct {
	BridgeName string
	Gateway    string
	Dns        []string
	Mtu        int
	Iface      string
}

type VmSpecs struct {
	Vcpu              int
	Memory            int
	Ip                string
	AdditionalVolumes []int
	AdditionalNICs    []UdpNic
}

type UdpNic struct {
	Local  int
	Remote int
}

type VolumeCreationFromBaseArgs struct {
	Name           string
	BaseVolumeName pulumi.StringOutput
	Pool           pulumi.StringOutput
	Format         string
	Size           pulumi.IntPtrInput
}

type VolumeCreationArgs struct {
	Name   string
	Source string
	Pool   pulumi.StringOutput
	Format string
	Size   pulumi.IntPtrInput
}

type NetrisController struct {
	URL              string
	Login            string
	Pass             string
	Insecure         bool
	Site             string
	CreateEmptyPorts bool
	CloudsimInternal bool
}

type NetrisInfo struct {
	Links            []link.Link
	BGPLinks         []link.Link
	Hardware         []inventory.HW
	SiteName         string
	MGMTSubnets      []ipam.IPAM
	ControllerInfo   NetrisControllerInfo
	SiteObject       site.Site
	allPortsBySwName map[string]map[string]interface{}
}

type NetrisControllerInfo struct {
	Version string
	AuthKey string
	URL     string
	AptRepo string
}

type VMResources struct {
	memory int
	vcpu   int
}

type LinkMapping struct {
	LocalID     int
	Local       string
	Remote      string
	RemoteID    int
	LocalIP     string
	RemoteIP    string
	BGPLocalIP  string
	BGPRemoteIP string
}

type ToTemplate struct {
	VmName           string
	Links            []LinkMapping
	CreateEmptyPorts bool
}
