package main

import (
	"strings"
	"testing"
)

func TestSortLinkMappingsByNumericLocalPort(t *testing.T) {
	links := []LinkMapping{
		{LocalPortIndex: 10, Local: "eth10"},
		{LocalPortIndex: 9, Local: "eth9"},
		{LocalPortIndex: 11, Local: "eth11"},
	}

	sortLinkMappings(links)

	got := []string{links[0].Local, links[1].Local, links[2].Local}
	want := []string{"eth9", "eth10", "eth11"}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("link order = %v, want %v", got, want)
		}
	}
}

func TestControllerInfoUsesConfiguredBackendVersion(t *testing.T) {
	info := controllerInfo(
		NetrisController{BackendVersion: "4.16.0-008"},
		"auth-key",
		"main",
	)

	if info.Version != "4.16.0-008" {
		t.Fatalf("expected configured backend version 4.16.0-008, got %q", info.Version)
	}
}

func TestPrepareCloudInitSGDoesNotRunNetworkScriptFromBootcmd(t *testing.T) {
	cloudInit := prepareCloudInitSG(map[string]interface{}{
		"hostname":          "softgate",
		"passwordHash":      "hash",
		"sshAuthKey":        []string{"key"},
		"installedPackages": []string{"lldpd"},
		"dnsServer":         "10.0.0.1",
		"ctlInfo": NetrisControllerInfo{
			Version: "4.16.0-001",
			AuthKey: "auth-key",
			AptRepo: "main",
		},
		"allVms": [][]map[string]string{{
			{
				"Type":        "softgate",
				"Name":        "ns-softgate-0",
				"MainAddress": "10.2.3.1",
				"MgmtAddress": "10.3.3.1/24",
			},
		}},
	}, false)

	bootcmdStart := strings.Index(cloudInit, "bootcmd:")
	runcmdStart := -1
	writeFilesStart := -1
	if bootcmdStart >= 0 {
		runcmdStart = strings.Index(cloudInit[bootcmdStart:], "runcmd:")
		if runcmdStart >= 0 {
			runcmdStart += bootcmdStart
		}
		writeFilesStart = strings.Index(cloudInit[bootcmdStart:], "write_files:")
		if writeFilesStart >= 0 {
			writeFilesStart += bootcmdStart
		}
	}
	if bootcmdStart < 0 || runcmdStart < 0 || writeFilesStart < 0 || runcmdStart >= writeFilesStart {
		t.Fatalf("cloud-init is missing the expected bootcmd/runcmd/write_files sections")
	}

	if strings.Contains(cloudInit[bootcmdStart:runcmdStart], "network_nics_up.sh") {
		t.Fatal("softgate bootcmd must not execute network_nics_up.sh")
	}

	if count := strings.Count(cloudInit[runcmdStart:writeFilesStart], "bash /etc/network_nics_up.sh"); count != 1 {
		t.Fatalf("network_nics_up.sh invocation count = %d, want 1", count)
	}
}
