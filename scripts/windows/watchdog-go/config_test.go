package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestDefaultPackagedExePrefersExplicitRepoRoot(t *testing.T) {
	local := t.TempDir()
	repoRoot := t.TempDir()
	t.Setenv("LOCALAPPDATA", local)

	localExe := filepath.Join(
		local,
		"hermes",
		"hermes-agent",
		"apps",
		"desktop",
		"release",
		"win-unpacked",
		"Hermes.exe",
	)
	repoExe := filepath.Join(
		repoRoot,
		"apps",
		"desktop",
		"release",
		"win-unpacked",
		"Hermes.exe",
	)
	for _, executable := range []string{localExe, repoExe} {
		if err := os.MkdirAll(filepath.Dir(executable), 0o755); err != nil {
			t.Fatalf("create executable directory: %v", err)
		}
		if err := os.WriteFile(executable, []byte("test"), 0o644); err != nil {
			t.Fatalf("create executable: %v", err)
		}
	}

	if got := defaultPackagedExe(repoRoot); got != repoExe {
		t.Fatalf("defaultPackagedExe() = %q, want %q", got, repoExe)
	}
}
