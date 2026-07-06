# Vimeo MCP Server

A Model Context Protocol (MCP) server that provides tools for interacting with the Vimeo API. This server allows you to manage videos, transcripts, and retrieve video statistics through a standardized interface.

## Features

- **List Videos**: Search and filter videos in your Vimeo account
- **Get Video Details**: Retrieve comprehensive metadata and statistics for specific videos
- **Update Videos**: Modify video titles, descriptions, tags, and privacy settings
- **Manage Transcripts**: Download and upload video transcripts/captions in multiple formats
- **Video Statistics**: Access play counts and other video metrics

## Installation

```bash
npm install @mcp/vimeo
```

Or install from source:

```bash
git clone <repository-url>
cd vimeo-mcp
npm install
npm run build
```

## Configuration

### Authentication

The Vimeo MCP server supports two authentication methods:

#### 1. Personal Access Token (Recommended)

Create a personal access token in your [Vimeo Developer Settings](https://developer.vimeo.com/apps) with the following scopes:
- `private` - Access private videos
- `edit` - Edit video metadata
- `upload` - Upload transcripts
- `stats` - View video statistics

Set the environment variable:
```bash
export VIMEO_ACCESS_TOKEN="your_access_token_here"
```

#### 2. OAuth2 Authentication

For OAuth2 authentication, you'll need:
```bash
export VIMEO_CLIENT_ID="your_client_id"
export VIMEO_CLIENT_SECRET="your_client_secret"
```

### Claude Desktop Configuration

Add the following to your Claude Desktop configuration file:

**macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
**Windows**: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "vimeo": {
      "command": "node",
      "args": ["/path/to/vimeo-mcp/dist/index.js"],
      "env": {
        "VIMEO_ACCESS_TOKEN": "your_access_token_here"
      }
    }
  }
}
```

## Available Tools

### vimeo_list_videos

List videos from your Vimeo account with optional filtering.

**Parameters:**
- `page` (number, optional): Page number (default: 1)
- `per_page` (number, optional): Results per page, max 100 (default: 25)
- `query` (string, optional): Search query
- `sort` (string, optional): Sort order - "date", "alphabetical", "plays", "likes", "duration"
- `direction` (string, optional): Sort direction - "asc" or "desc"

**Example:**
```
List my most played videos
```

### vimeo_get_video_details

Get detailed information about a specific video.

**Parameters:**
- `video_id` (string, required): The ID of the video

**Example:**
```
Get details for video 123456789
```

### vimeo_update_video

Update video metadata.

**Parameters:**
- `video_id` (string, required): The ID of the video to update
- `title` (string, optional): New video title
- `description` (string, optional): New video description
- `tags` (array, optional): Array of tag strings
- `privacy` (string, optional): Privacy setting - "anybody", "nobody", "password", "disable"

**Example:**
```
Update video 123456789 with title "My New Title" and tags ["tutorial", "coding"]
```

### vimeo_download_transcript

Download video transcript or captions.

**Parameters:**
- `video_id` (string, required): The ID of the video
- `language` (string, optional): Language code (default: "en")
- `format` (string, optional): Format - "webvtt" or "srt" (default: "webvtt")

**Example:**
```
Download English transcript for video 123456789 in SRT format
```

### vimeo_upload_transcript

Upload a new transcript or captions for a video.

**Parameters:**
- `video_id` (string, required): The ID of the video
- `content` (string, required): The transcript content
- `language` (string, optional): Language code (default: "en")
- `format` (string, optional): Format - "webvtt" or "srt" (default: "webvtt")

**Example:**
```
Upload this transcript for video 123456789:
WEBVTT

00:00:00.000 --> 00:00:05.000
Hello, welcome to my video.

00:00:05.000 --> 00:00:10.000
Today we'll be learning about MCP servers.
```

### vimeo_get_video_stats

Get statistics for a video.

**Parameters:**
- `video_id` (string, required): The ID of the video

**Example:**
```
Get stats for video 123456789
```

## Error Handling

The server implements comprehensive error handling:

- **Rate Limiting**: Automatically detects rate limit errors and provides retry information
- **API Errors**: Detailed error messages for failed API calls
- **Network Errors**: Graceful handling of connection issues
- **Validation Errors**: Clear messages for invalid parameters

## Rate Limiting

Vimeo's API has rate limits. The server handles these automatically by:
- Detecting 429 (Too Many Requests) responses
- Extracting retry-after headers
- Providing clear feedback about when to retry

## Development

```bash
# Install dependencies
npm install

# Build the project
npm run build

# Run in development mode
npm run dev

# Watch for changes
npm run watch
```

## License

MIT

## Support

For issues and feature requests, please visit the [GitHub repository](https://github.com/your-username/vimeo-mcp).

For Vimeo API documentation, see: https://developer.vimeo.com/api/reference