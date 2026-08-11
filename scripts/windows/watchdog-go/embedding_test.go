package main

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestBuildEmbeddingCommandUsesConfiguredLoopbackEndpoint(t *testing.T) {
	dir := t.TempDir()
	serverPath := filepath.Join(dir, "llama-server.exe")
	modelPath := filepath.Join(dir, "bge.gguf")
	for _, path := range []string{serverPath, modelPath} {
		if err := os.WriteFile(path, []byte("stub"), 0o644); err != nil {
			t.Fatal(err)
		}
	}

	cfg := Config{
		EmbeddingEnabled:  true,
		EmbeddingEndpoint: "http://127.0.0.1:18082",
		EmbeddingServer:   serverPath,
		EmbeddingModel:    modelPath,
		EmbeddingArgsJSON: `[
			"--alias", "nsfw-bge-m3-v5-q6_k", "--embedding",
			"--ctx-size", "8192", "--n-gpu-layers", "99"
		]`,
	}

	cmd, endpoint, err := buildEmbeddingCommand(cfg)
	if err != nil {
		t.Fatal(err)
	}
	if endpoint != "http://127.0.0.1:18082" {
		t.Fatalf("endpoint = %q", endpoint)
	}
	if cmd.Path != serverPath {
		t.Fatalf("server = %q, want %q", cmd.Path, serverPath)
	}
	joined := strings.Join(cmd.Args, " ")
	for _, want := range []string{
		"--model " + modelPath,
		"--host 127.0.0.1",
		"--port 18082",
		"--alias nsfw-bge-m3-v5-q6_k",
		"--embedding",
		"--n-gpu-layers 99",
	} {
		if !strings.Contains(joined, want) {
			t.Fatalf("missing %q in %q", want, joined)
		}
	}
}

func TestBuildEmbeddingCommandExcludesIncompatibleCacheTypeVEnvironment(t *testing.T) {
	t.Setenv("LLAMA_ARG_CACHE_TYPE_V", "turbo3")
	dir := t.TempDir()
	serverPath := filepath.Join(dir, "llama-server.exe")
	modelPath := filepath.Join(dir, "bge.gguf")
	for _, path := range []string{serverPath, modelPath} {
		if err := os.WriteFile(path, []byte("stub"), 0o644); err != nil {
			t.Fatal(err)
		}
	}

	cmd, _, err := buildEmbeddingCommand(Config{
		EmbeddingEndpoint: "http://127.0.0.1:18082",
		EmbeddingServer:   serverPath,
		EmbeddingModel:    modelPath,
		EmbeddingArgsJSON: `["--embedding"]`,
	})
	if err != nil {
		t.Fatal(err)
	}
	if cmd.Env == nil {
		t.Fatal("embedding command inherited the incompatible cache environment")
	}
	for _, item := range cmd.Env {
		if strings.HasPrefix(item, "LLAMA_ARG_CACHE_TYPE_V=") {
			t.Fatalf("embedding command leaked incompatible environment %q", item)
		}
	}
}

func TestBuildEmbeddingCommandRejectsRemoteEndpoint(t *testing.T) {
	cfg := Config{
		EmbeddingEnabled:  true,
		EmbeddingEndpoint: "http://192.0.2.10:8082",
		EmbeddingServer:   `C:\\llama-server.exe`,
		EmbeddingModel:    `C:\\model.gguf`,
		EmbeddingArgsJSON: `[]`,
	}

	if _, _, err := buildEmbeddingCommand(cfg); err == nil {
		t.Fatal("remote embedding endpoint must be rejected")
	}
}

func TestParseEmbeddingArgsRequiresEmbeddingMode(t *testing.T) {
	if _, err := parseEmbeddingArgs(`["--alias", "nsfw-bge-m3-v5-q6_k"]`); err == nil {
		t.Fatal("embedding launch must require --embedding")
	}
}

func TestEmbeddingEndpointHealthyUsesHealthPath(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/health" {
			http.NotFound(w, r)
			return
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	if !embeddingEndpointHealthy(server.URL) {
		t.Fatal("expected healthy loopback endpoint")
	}
}

func TestEnsureEmbeddingHealthyPreservesHealthyExternalEndpointWithoutLaunchConfig(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/health" {
			http.NotFound(w, r)
			return
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	wd := NewWatchdog(Config{
		EmbeddingEnabled:  true,
		EmbeddingEndpoint: server.URL,
	}, NewLogger(filepath.Join(t.TempDir(), "watchdog.log")))

	status, _ := wd.ensureEmbeddingHealthy()
	if status != "up" {
		t.Fatalf("status = %q, want up", status)
	}
}
