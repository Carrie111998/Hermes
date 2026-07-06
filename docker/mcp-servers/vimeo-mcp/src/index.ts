#!/usr/bin/env node

import { config } from "dotenv";
config();

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
  Tool,
} from "@modelcontextprotocol/sdk/types.js";
import { z } from "zod";
import { VimeoClient } from "./vimeo-client.js";
import { VimeoError, RateLimitError } from "./errors.js";

// Schema definitions
const ListVideosSchema = z.object({
  page: z.number().min(1).default(1).optional(),
  per_page: z.number().min(1).max(15).default(6).optional().describe("Max 15 for compact mode, 8 for full mode"),
  query: z.string().optional(),
  sort: z.enum(["date", "alphabetical", "plays", "likes", "duration"]).optional(),
  direction: z.enum(["asc", "desc"]).optional(),
  compact: z.boolean().default(true).optional().describe("Use compact response mode to reduce token usage (recommended)"),
  fields: z.array(z.string()).optional().describe("Custom fields to include (overrides compact mode)"),
});

const GetVideoDetailsSchema = z.object({
  video_id: z.string().describe("The ID of the video (e.g., '123456789')"),
});

const UpdateVideoSchema = z.object({
  video_id: z.string().describe("The ID of the video to update"),
  title: z.string().optional(),
  description: z.string().optional(),
  tags: z.array(z.string()).optional(),
  privacy: z.enum(["anybody", "nobody", "password", "disable"]).optional(),
});

const TranscriptSchema = z.object({
  video_id: z.string().describe("The ID of the video"),
  language: z.string().default("en").optional(),
  format: z.enum(["webvtt", "srt"]).default("webvtt").optional(),
});

const UploadTranscriptSchema = z.object({
  video_id: z.string().describe("The ID of the video"),
  content: z.string().describe("The transcript content"),
  language: z.string().default("en"),
  format: z.enum(["webvtt", "srt"]).default("webvtt"),
});

const DownloadTranscriptToFileSchema = z.object({
  video_id: z.string().describe("The ID of the video"),
  language: z.string().default("en").optional(),
  format: z.enum(["webvtt", "srt"]).default("webvtt").optional(),
  base_path: z.string().default("./downloads/vimeo").optional().describe("Base directory path for saving files (relative to current working directory)"),
});

const GenerateContentAnalysisSchema = z.object({
  video_id: z.string().describe("The ID of the video"),
  base_path: z.string().default("./downloads/vimeo").optional().describe("Base directory path for saving files (relative to current working directory)"),
});

// Tool definitions
const TOOLS: Tool[] = [
  {
    name: "vimeo_health_check",
    description: "Check the health and connectivity of the Vimeo MCP server",
    inputSchema: {
      type: "object",
      properties: {},
    },
  },
  {
    name: "vimeo_validate_video",
    description: "Validate a video (required for some screen recordings before updates)",
    inputSchema: {
      type: "object",
      properties: {
        video_id: { 
          type: "string", 
          description: "The ID of the video to validate"
        },
      },
      required: ["video_id"],
    },
  },
  {
    name: "vimeo_list_videos_minimal",
    description: "List videos with minimal data to avoid token limits. Returns only ID, name, and duration. Use this for finding videos quickly.",
    inputSchema: {
      type: "object",
      properties: {
        page: { type: "number", minimum: 1, default: 1 },
        per_page: { 
          type: "number", 
          minimum: 1, 
          maximum: 20, 
          default: 10,
          description: "Number of videos to return (max 20 for minimal mode)"
        },
        query: { type: "string", description: "Search query" },
        sort: { 
          type: "string", 
          enum: ["date", "alphabetical", "plays", "likes", "duration"],
          description: "Sort order for the results"
        },
        direction: { 
          type: "string", 
          enum: ["asc", "desc"],
          description: "Sort direction"
        },
      },
    },
  },
  {
    name: "vimeo_list_videos",
    description: "List videos from the authenticated user's account with optional filtering. OPTIMIZED: Uses compact mode by default to prevent token overflow (reduced from 42K+ to ~4K tokens)",
    inputSchema: {
      type: "object",
      properties: {
        page: { type: "number", minimum: 1, default: 1 },
        per_page: { 
          type: "number", 
          minimum: 1, 
          maximum: 15, 
          default: 6,
          description: "Max 15 for compact mode, 8 for full mode. Reduced from 25 to prevent token overflow."
        },
        query: { type: "string", description: "Search query" },
        sort: { 
          type: "string", 
          enum: ["date", "alphabetical", "plays", "likes", "duration"],
          description: "Sort order for the results"
        },
        direction: { 
          type: "string", 
          enum: ["asc", "desc"],
          description: "Sort direction"
        },
        compact: {
          type: "boolean",
          default: true,
          description: "Use compact response mode to reduce token usage (recommended). Includes: uri, name, description, duration, created_time, privacy, stats, tags"
        },
        fields: {
          type: "array",
          items: { type: "string" },
          description: "Custom fields to include (overrides compact mode). Use dot notation for nested fields like 'privacy.view'"
        },
      },
    },
  },
  {
    name: "vimeo_get_video_details",
    description: "Get detailed information about a specific video including metadata and basic statistics",
    inputSchema: {
      type: "object",
      properties: {
        video_id: { 
          type: "string", 
          description: "The ID of the video (e.g., '123456789')"
        },
      },
      required: ["video_id"],
    },
  },
  {
    name: "vimeo_update_video",
    description: "Update video metadata including title, description, tags, and privacy settings",
    inputSchema: {
      type: "object",
      properties: {
        video_id: { 
          type: "string", 
          description: "The ID of the video to update"
        },
        title: { type: "string" },
        description: { type: "string" },
        tags: { 
          type: "array", 
          items: { type: "string" },
          description: "Array of tags"
        },
        privacy: { 
          type: "string", 
          enum: ["anybody", "nobody", "password", "disable"],
          description: "Privacy setting for the video"
        },
      },
      required: ["video_id"],
    },
  },
  {
    name: "vimeo_download_transcript",
    description: "Download video transcript or captions if available",
    inputSchema: {
      type: "object",
      properties: {
        video_id: { 
          type: "string", 
          description: "The ID of the video"
        },
        language: { 
          type: "string", 
          default: "en",
          description: "Language code (e.g., 'en', 'es', 'fr')"
        },
        format: { 
          type: "string", 
          enum: ["webvtt", "srt"],
          default: "webvtt",
          description: "Transcript format"
        },
      },
      required: ["video_id"],
    },
  },
  {
    name: "vimeo_upload_transcript",
    description: "Upload a new transcript or captions for a video",
    inputSchema: {
      type: "object",
      properties: {
        video_id: { 
          type: "string", 
          description: "The ID of the video"
        },
        content: { 
          type: "string", 
          description: "The transcript content in the specified format"
        },
        language: { 
          type: "string", 
          default: "en",
          description: "Language code (e.g., 'en', 'es', 'fr')"
        },
        format: { 
          type: "string", 
          enum: ["webvtt", "srt"],
          default: "webvtt",
          description: "Transcript format"
        },
      },
      required: ["video_id", "content"],
    },
  },
  {
    name: "vimeo_get_video_stats",
    description: "Get basic statistics for a video including play count",
    inputSchema: {
      type: "object",
      properties: {
        video_id: { 
          type: "string", 
          description: "The ID of the video"
        },
      },
      required: ["video_id"],
    },
  },
  {
    name: "vimeo_download_transcript_to_file",
    description: "Download video transcript and save to organized file structure with date-based folders",
    inputSchema: {
      type: "object",
      properties: {
        video_id: { 
          type: "string", 
          description: "The ID of the video"
        },
        language: { 
          type: "string", 
          default: "en",
          description: "Language code (e.g., 'en', 'es', 'fr')"
        },
        format: { 
          type: "string", 
          enum: ["webvtt", "srt"],
          default: "webvtt",
          description: "Transcript format"
        },
        base_path: { 
          type: "string", 
          default: "./downloads/vimeo",
          description: "Base directory path for saving files (relative to current working directory)"
        },
      },
      required: ["video_id"],
    },
  },
  {
    name: "vimeo_generate_content_analysis",
    description: "Generate comprehensive content analysis and title/description suggestions as markdown file",
    inputSchema: {
      type: "object",
      properties: {
        video_id: { 
          type: "string", 
          description: "The ID of the video"
        },
        base_path: { 
          type: "string", 
          default: "./downloads/vimeo",
          description: "Base directory path for saving files (relative to current working directory)"
        },
      },
      required: ["video_id"],
    },
  },
];

class VimeoMCPServer {
  private server: Server;
  private vimeoClient?: VimeoClient;

  constructor() {
    this.server = new Server(
      {
        name: "vimeo-mcp",
        version: "0.1.0",
      },
      {
        capabilities: {
          tools: {},
        },
      }
    );

    this.setupHandlers();
  }

  private setupHandlers() {
    this.server.setRequestHandler(ListToolsRequestSchema, async () => ({
      tools: TOOLS,
    }));

    this.server.setRequestHandler(CallToolRequestSchema, async (request) => {
      const { name, arguments: args } = request.params;

      if (!this.vimeoClient) {
        this.initializeClient();
      }

      if (!this.vimeoClient) {
        throw new Error("Failed to initialize Vimeo client. Please check your credentials.");
      }

      try {
        switch (name) {
          case "vimeo_health_check":
            return await this.handleHealthCheck(args);
          case "vimeo_validate_video":
            return await this.handleValidateVideo(args);
          case "vimeo_list_videos_minimal":
            return await this.handleListVideosMinimal(args);
          case "vimeo_list_videos":
            return await this.handleListVideos(args);
          case "vimeo_get_video_details":
            return await this.handleGetVideoDetails(args);
          case "vimeo_update_video":
            return await this.handleUpdateVideo(args);
          case "vimeo_download_transcript":
            return await this.handleDownloadTranscript(args);
          case "vimeo_upload_transcript":
            return await this.handleUploadTranscript(args);
          case "vimeo_get_video_stats":
            return await this.handleGetVideoStats(args);
          case "vimeo_download_transcript_to_file":
            return await this.handleDownloadTranscriptToFile(args);
          case "vimeo_generate_content_analysis":
            return await this.handleGenerateContentAnalysis(args);
          default:
            throw new Error(`Unknown tool: ${name}`);
        }
      } catch (error) {
        if (error instanceof RateLimitError) {
          return {
            content: [
              {
                type: "text",
                text: `Rate limit exceeded. Please wait ${error.retryAfter} seconds before retrying.`,
              },
            ],
            isError: true,
          };
        }
        if (error instanceof VimeoError) {
          return {
            content: [
              {
                type: "text",
                text: `Vimeo API error: ${error.message}`,
              },
            ],
            isError: true,
          };
        }
        throw error;
      }
    });
  }

  private initializeClient() {
    // Check for OAuth2 credentials first
    const clientId = process.env.VIMEO_CLIENT_ID;
    const clientSecret = process.env.VIMEO_CLIENT_SECRET;
    const accessToken = process.env.VIMEO_ACCESS_TOKEN;

    if (!accessToken && (!clientId || !clientSecret)) {
      throw new Error(
        "Vimeo credentials not configured. Please set either VIMEO_ACCESS_TOKEN or both VIMEO_CLIENT_ID and VIMEO_CLIENT_SECRET environment variables."
      );
    }

    try {
      this.vimeoClient = new VimeoClient({
        clientId,
        clientSecret,
        accessToken,
      });
      
      // Test the connection with a simple API call
      this.validateConnection();
    } catch (error) {
      throw new Error(`Failed to initialize Vimeo client: ${error instanceof Error ? error.message : 'Unknown error'}`);
    }
  }

  private async validateConnection() {
    if (!this.vimeoClient) return;
    
    try {
      // Make a simple API call to validate credentials
      await this.vimeoClient.listVideos({ per_page: 1, page: 1 });
    } catch (error) {
      console.warn("Warning: Vimeo credentials may be invalid or expired. Some operations may fail.");
    }
  }

  private async handleHealthCheck(_args: unknown) {
    const status = {
      server: "Vimeo MCP Server",
      version: "0.1.0",
      status: "healthy",
      timestamp: new Date().toISOString(),
      credentials: {
        access_token: !!process.env.VIMEO_ACCESS_TOKEN,
        client_id: !!process.env.VIMEO_CLIENT_ID,
        client_secret: !!process.env.VIMEO_CLIENT_SECRET,
      },
      connection: "unknown"
    };

    // Test connection if credentials are available
    if (this.vimeoClient) {
      try {
        await this.vimeoClient.listVideos({ per_page: 1, page: 1 });
        status.connection = "connected";
      } catch (error) {
        status.connection = "failed";
        status.status = "degraded";
      }
    } else {
      status.status = "error";
      status.connection = "no_credentials";
    }

    return {
      content: [
        {
          type: "text",
          text: JSON.stringify(status, null, 2),
        },
      ],
    };
  }

  private async handleValidateVideo(args: unknown) {
    const { video_id } = GetVideoDetailsSchema.parse(args);
    const result = await this.vimeoClient!.validateVideo(video_id);

    return {
      content: [
        {
          type: "text",
          text: `Video ${video_id} validated successfully. Result: ${JSON.stringify(result, null, 2)}`,
        },
      ],
    };
  }

  private async handleListVideosMinimal(args: unknown) {
    const params = ListVideosSchema.parse(args);
    
    // Force minimal mode with restricted fields
    const minimalParams = {
      ...params,
      fields: ['uri', 'name', 'duration', 'created_time', 'privacy.view'],
      per_page: Math.min(params.per_page || 10, 20)
    };
    
    const videos = await this.vimeoClient!.listVideos(minimalParams);

    return {
      content: [
        {
          type: "text",
          text: JSON.stringify(videos, null, 2),
        },
      ],
    };
  }

  private async handleListVideos(args: unknown) {
    const params = ListVideosSchema.parse(args);
    const videos = await this.vimeoClient!.listVideos(params);

    return {
      content: [
        {
          type: "text",
          text: JSON.stringify(videos, null, 2),
        },
      ],
    };
  }

  private async handleGetVideoDetails(args: unknown) {
    const { video_id } = GetVideoDetailsSchema.parse(args);
    const details = await this.vimeoClient!.getVideoDetails(video_id);

    return {
      content: [
        {
          type: "text",
          text: JSON.stringify(details, null, 2),
        },
      ],
    };
  }

  private async handleUpdateVideo(args: unknown) {
    const params = UpdateVideoSchema.parse(args);
    
    // Try validating the video first (for screen recordings)
    try {
      await this.vimeoClient!.validateVideo(params.video_id);
      console.log("Video validated successfully");
    } catch (error) {
      // If validation fails, continue anyway
      console.error("Validation failed, continuing with update:", error);
    }
    
    let titleUpdated = false;
    let descriptionUpdated = false;
    let tagsUpdated = false;
    let privacyUpdated = false;
    const errors: string[] = [];
    
    // Update title separately if provided
    if (params.title) {
      try {
        await this.vimeoClient!.updateVideoTitle(params.video_id, params.title);
        titleUpdated = true;
        console.log("Title updated successfully");
      } catch (error) {
        console.error("Failed to update title:", error);
        errors.push(`Title update failed: ${error}`);
      }
    }
    
    // Update description separately if provided
    if (params.description) {
      try {
        await this.vimeoClient!.updateVideoDescription(params.video_id, params.description);
        descriptionUpdated = true;
        console.log("Description updated successfully");
      } catch (error) {
        console.error("Failed to update description:", error);
        errors.push(`Description update failed: ${error}`);
      }
    }
    
    // Update privacy if provided
    if (params.privacy) {
      try {
        await this.vimeoClient!.updateVideo(params.video_id, {
          privacy: { view: params.privacy }
        });
        privacyUpdated = true;
        console.log("Privacy updated successfully");
      } catch (error) {
        console.error("Failed to update privacy:", error);
        errors.push(`Privacy update failed: ${error}`);
      }
    }

    // Handle tags separately if provided
    if (params.tags) {
      try {
        await this.vimeoClient!.setVideoTags(params.video_id, params.tags);
        tagsUpdated = true;
        console.log("Tags updated successfully");
      } catch (error) {
        console.error("Failed to update tags:", error);
        errors.push(`Tags update failed: ${error}`);
      }
    }
    
    // Get updated video details
    const updated = await this.vimeoClient!.getVideoDetails(params.video_id);

    const statusMessage = [
      "Video update results:",
      titleUpdated ? "✅ Title updated" : params.title ? "❌ Title update failed" : "⏭️ Title not provided",
      descriptionUpdated ? "✅ Description updated" : params.description ? "❌ Description update failed" : "⏭️ Description not provided",
      tagsUpdated ? "✅ Tags updated" : params.tags ? "❌ Tags update failed" : "⏭️ Tags not provided",
      privacyUpdated ? "✅ Privacy updated" : params.privacy ? "❌ Privacy update failed" : "⏭️ Privacy not provided",
    ].join("\n");
    
    const errorMessage = errors.length > 0 ? `\n\nErrors encountered:\n${errors.join("\n")}` : "";

    return {
      content: [
        {
          type: "text",
          text: `${statusMessage}${errorMessage}\n\nCurrent video details:\n${JSON.stringify(updated, null, 2)}`,
        },
      ],
    };
  }

  private async handleDownloadTranscript(args: unknown) {
    const { video_id, language, format } = TranscriptSchema.parse(args);
    const transcript = await this.vimeoClient!.downloadTranscript(video_id, language!, format!);

    return {
      content: [
        {
          type: "text",
          text: transcript,
        },
      ],
    };
  }

  private async handleUploadTranscript(args: unknown) {
    const params = UploadTranscriptSchema.parse(args);
    await this.vimeoClient!.uploadTranscript(
      params.video_id,
      params.content,
      params.language,
      params.format
    );

    return {
      content: [
        {
          type: "text",
          text: `Transcript uploaded successfully for video ${params.video_id} in ${params.language}.`,
        },
      ],
    };
  }

  private async handleGetVideoStats(args: unknown) {
    const { video_id } = GetVideoDetailsSchema.parse(args);
    const stats = await this.vimeoClient!.getVideoStats(video_id);

    return {
      content: [
        {
          type: "text",
          text: JSON.stringify(stats, null, 2),
        },
      ],
    };
  }

  private async handleDownloadTranscriptToFile(args: unknown) {
    const params = DownloadTranscriptToFileSchema.parse(args);
    const filePath = await this.vimeoClient!.downloadTranscriptToFile(
      params.video_id,
      params.language,
      params.format,
      params.base_path
    );

    return {
      content: [
        {
          type: "text",
          text: `Transcript downloaded successfully to: ${filePath}`,
        },
      ],
    };
  }

  private async handleGenerateContentAnalysis(args: unknown) {
    const params = GenerateContentAnalysisSchema.parse(args);
    const filePath = await this.vimeoClient!.generateContentAnalysisFile(
      params.video_id,
      params.base_path
    );

    return {
      content: [
        {
          type: "text",
          text: `Content analysis and suggestions generated successfully to: ${filePath}`,
        },
      ],
    };
  }

  async run() {
    const transport = new StdioServerTransport();
    await this.server.connect(transport);
    console.error("Vimeo MCP server running on stdio");
  }
}

// Main entry point
const server = new VimeoMCPServer();
server.run().catch((error) => {
  console.error("Fatal error:", error);
  process.exit(1);
});