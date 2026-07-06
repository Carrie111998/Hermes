import { Vimeo } from "@vimeo/vimeo";
import { VimeoError, RateLimitError } from "./errors.js";
import fs from "fs";
import path from "path";

interface VimeoClientConfig {
  clientId?: string;
  clientSecret?: string;
  accessToken?: string;
}

interface ListVideosParams {
  page?: number;
  per_page?: number;
  query?: string;
  sort?: string;
  direction?: string;
  compact?: boolean;
  fields?: string[];
}

type VideoResponseMode = 'full' | 'compact' | 'minimal';

interface VideoFilterOptions {
  mode: VideoResponseMode;
  customFields?: string[];
}

interface VideoUpdateParams {
  name?: string;
  description?: string;
  privacy?: {
    view: string;
  };
}

interface ContentAnalysis {
  contentType: string;
  codingScore: number;
  motivationScore: number;
  keyTopics: string[];
  keywords: { [key: string]: number };
  sentiment: string;
  sessionType: string;
  technicalLevel: string;
}

// Response filtering utility functions
class VideoResponseFilter {
  private static readonly MINIMAL_FIELDS = [
    'uri', 'name', 'duration', 'created_time', 'privacy.view'
  ];

  private static readonly COMPACT_FIELDS = [
    'uri', 'name', 'description', 'duration', 'created_time', 'modified_time',
    'privacy.view', 'privacy.embed', 'stats.plays', 'tags'
  ];

  static filterVideoResponse(video: any, options: VideoFilterOptions): any {
    switch (options.mode) {
      case 'minimal':
        return this.extractFields(video, this.MINIMAL_FIELDS);
      case 'compact':
        return this.extractFields(video, this.COMPACT_FIELDS);
      case 'full':
        return this.sanitizeFullResponse(video);
      default:
        return options.customFields 
          ? this.extractFields(video, options.customFields)
          : this.sanitizeFullResponse(video);
    }
  }

  private static extractFields(obj: any, fieldPaths: string[]): any {
    const result: any = {};
    
    for (const fieldPath of fieldPaths) {
      const keys = fieldPath.split('.');
      let sourceValue = obj;
      let targetObj = result;
      
      // Navigate through the object path
      for (let i = 0; i < keys.length - 1; i++) {
        const key = keys[i];
        if (sourceValue?.[key] === undefined) break;
        
        sourceValue = sourceValue[key];
        if (!targetObj[key]) targetObj[key] = {};
        targetObj = targetObj[key];
      }
      
      // Set the final value
      const finalKey = keys[keys.length - 1];
      if (sourceValue?.[finalKey] !== undefined) {
        targetObj[finalKey] = sourceValue[finalKey];
      }
    }
    
    return result;
  }

  private static sanitizeFullResponse(video: any): any {
    // Remove the most verbose fields that consume tokens
    const sanitized = { ...video };
    
    // Reduce picture sizes to only essential ones
    if (sanitized.pictures?.sizes) {
      sanitized.pictures.sizes = sanitized.pictures.sizes.filter((size: any) => 
        [200, 640, 1280].includes(size.width)
      ).map((size: any) => ({
        width: size.width,
        height: size.height,
        link: size.link
      }));
    }

    // Remove verbose metadata that's rarely needed
    delete sanitized.metadata?.connections?.comments;
    delete sanitized.metadata?.connections?.likes;
    delete sanitized.metadata?.connections?.pictures;
    delete sanitized.metadata?.connections?.texttracks;
    delete sanitized.metadata?.connections?.chapters;

    // Keep only essential embed data
    if (sanitized.embed) {
      sanitized.embed = {
        html: sanitized.embed.html
      };
    }

    return sanitized;
  }

  static estimateTokenCount(data: any): number {
    // Rough token estimation: ~4 characters per token
    const jsonString = JSON.stringify(data);
    return Math.ceil(jsonString.length / 4);
  }
}

export class VimeoClient {
  private client: any;
  private rateLimitReset?: number;

  constructor(config: VimeoClientConfig) {
    if (config.accessToken) {
      // Use Personal Access Token
      // Vendored patch: the @vimeo/vimeo constructor accepts null id/secret
      // for Personal-Access-Token usage; cast to satisfy tsc strict typing
      // without changing the runtime value.
      this.client = new Vimeo(null as unknown as string, null as unknown as string, config.accessToken);
    } else if (config.clientId && config.clientSecret) {
      // Use OAuth2
      this.client = new Vimeo(config.clientId, config.clientSecret);
    } else {
      throw new Error("Invalid configuration: provide either accessToken or clientId/clientSecret");
    }
  }

  private async executeWithRateLimit<T>(
    operation: () => Promise<T>
  ): Promise<T> {
    // Check if we're currently rate limited
    if (this.rateLimitReset && Date.now() < this.rateLimitReset) {
      const waitTime = Math.ceil((this.rateLimitReset - Date.now()) / 1000);
      throw new RateLimitError(
        `Rate limit exceeded. Please wait ${waitTime} seconds.`,
        waitTime
      );
    }

    try {
      const result = await operation();
      return result;
    } catch (error: any) {
      // Handle rate limit errors
      if (error.statusCode === 429) {
        const retryAfter = error.headers?.["retry-after"] || 60;
        this.rateLimitReset = Date.now() + retryAfter * 1000;
        throw new RateLimitError(
          `Rate limit exceeded. Please wait ${retryAfter} seconds.`,
          retryAfter
        );
      }

      // Handle other Vimeo API errors
      if (error.statusCode) {
        throw new VimeoError(
          error.message || `Vimeo API error (${error.statusCode})`,
          error.statusCode
        );
      }

      throw error;
    }
  }

  private makeApiCall(
    method: string,
    path: string,
    params?: any
  ): Promise<any> {
    return new Promise((resolve, reject) => {
      const callback = (error: any, body: any, statusCode: number, headers: any) => {
        if (error) {
          error.statusCode = statusCode;
          error.headers = headers;
          reject(error);
        } else {
          resolve(body);
        }
      };

      if (method === "GET") {
        this.client.request({ method, path, query: params }, callback);
      } else {
        // For non-GET requests, @vimeo/vimeo library uses 'query' field for body data
        // This is a known quirk of the library - see https://github.com/vimeo/vimeo.js/issues/186
        this.client.request({ method, path, query: params }, callback);
      }
    });
  }

  async listVideos(params: ListVideosParams) {
    // Intelligent defaults for token optimization
    const maxPageSize = params.compact ? 15 : 8;
    const defaultPageSize = params.compact ? 10 : 6;
    
    const queryParams: any = {
      page: params.page || 1,
      per_page: Math.min(params.per_page || defaultPageSize, maxPageSize),
    };

    if (params.query) {
      queryParams.query = params.query;
    }
    if (params.sort) {
      queryParams.sort = params.sort;
    }
    if (params.direction) {
      queryParams.direction = params.direction;
    }

    // Get raw response from API
    const response = await this.executeWithRateLimit(() =>
      this.makeApiCall("GET", "/me/videos", queryParams)
    );

    // Apply response filtering based on mode
    if (params.compact || params.fields) {
      const mode: VideoResponseMode = params.compact ? 'compact' : 'full';
      const filterOptions: VideoFilterOptions = {
        mode,
        customFields: params.fields
      };

      // Filter each video in the response
      if (response.data && Array.isArray(response.data)) {
        response.data = response.data.map((video: any) => 
          VideoResponseFilter.filterVideoResponse(video, filterOptions)
        );
      }

      // Add token estimation for monitoring
      const estimatedTokens = VideoResponseFilter.estimateTokenCount(response);
      response._meta = {
        ...response._meta,
        estimated_tokens: estimatedTokens,
        response_mode: mode,
        page_size_warning: queryParams.per_page > maxPageSize ? 
          `Page size reduced to ${maxPageSize} to prevent token overflow` : undefined
      };
    }

    return response;
  }

  async getVideoDetails(videoId: string) {
    const path = `/videos/${videoId}`;
    return this.executeWithRateLimit(() =>
      this.makeApiCall("GET", path, {
        fields: "uri,name,description,duration,created_time,modified_time,privacy,pictures,tags,stats.plays,metadata.connections.comments.total,metadata.connections.likes.total",
      })
    );
  }

  async updateVideo(videoId: string, params: VideoUpdateParams, usePut: boolean = false) {
    const path = `/videos/${videoId}`;
    const method = usePut ? "PUT" : "PATCH";
    return this.executeWithRateLimit(() =>
      this.makeApiCall(method, path, params)
    );
  }

  async updateVideoTitle(videoId: string, title: string) {
    const path = `/videos/${videoId}`;
    // Try different approaches
    const approaches = [
      // Approach 1: PATCH with just name
      () => this.makeApiCall("PATCH", path, { name: title }),
      // Approach 2: PUT with just name
      () => this.makeApiCall("PUT", path, { name: title }),
      // Approach 3: PATCH with full object
      () => this.makeApiCall("PATCH", path, { 
        name: title,
        review_page: { active: true }
      }),
      // Approach 4: Direct API call with different headers
      () => new Promise((resolve, reject) => {
        this.client.request({
          method: "PATCH",
          path: path,
          headers: {
            'Content-Type': 'application/json',
            'Accept': 'application/vnd.vimeo.*+json;version=3.4'
          },
          query: { name: title }
        }, (error: any, body: any) => {
          if (error) reject(error);
          else resolve(body);
        });
      })
    ];

    // Try each approach until one succeeds
    for (let i = 0; i < approaches.length; i++) {
      try {
        const result = await this.executeWithRateLimit(approaches[i]);
        console.log(`Title update succeeded with approach ${i + 1}`);
        return result;
      } catch (error) {
        console.error(`Title update approach ${i + 1} failed:`, error);
        if (i === approaches.length - 1) throw error;
      }
    }
  }

  async updateVideoDescription(videoId: string, description: string) {
    const path = `/videos/${videoId}`;
    // Try different approaches
    const approaches = [
      // Approach 1: PATCH with just description
      () => this.makeApiCall("PATCH", path, { description }),
      // Approach 2: PUT with just description
      () => this.makeApiCall("PUT", path, { description }),
      // Approach 3: PATCH with full object
      () => this.makeApiCall("PATCH", path, { 
        description,
        review_page: { active: true }
      }),
      // Approach 4: Direct API call with different headers
      () => new Promise((resolve, reject) => {
        this.client.request({
          method: "PATCH",
          path: path,
          headers: {
            'Content-Type': 'application/json',
            'Accept': 'application/vnd.vimeo.*+json;version=3.4'
          },
          query: { description }
        }, (error: any, body: any) => {
          if (error) reject(error);
          else resolve(body);
        });
      })
    ];

    // Try each approach until one succeeds
    for (let i = 0; i < approaches.length; i++) {
      try {
        const result = await this.executeWithRateLimit(approaches[i]);
        console.log(`Description update succeeded with approach ${i + 1}`);
        return result;
      } catch (error) {
        console.error(`Description update approach ${i + 1} failed:`, error);
        if (i === approaches.length - 1) throw error;
      }
    }
  }

  async validateVideo(videoId: string) {
    const path = `/videos/${videoId}/validate`;
    return this.executeWithRateLimit(() =>
      this.makeApiCall("PUT", path, {})
    );
  }

  async setVideoTags(videoId: string, tags: string[]) {
    const path = `/videos/${videoId}/tags`;
    
    // First, remove all existing tags
    const existingTags = await this.executeWithRateLimit(() =>
      this.makeApiCall("GET", path)
    );

    // Remove existing tags
    for (const tag of existingTags.data || []) {
      await this.executeWithRateLimit(() =>
        this.makeApiCall("DELETE", `${path}/${encodeURIComponent(tag.tag)}`)
      );
    }

    // Add new tags
    for (const tag of tags) {
      await this.executeWithRateLimit(() =>
        this.makeApiCall("PUT", `${path}/${encodeURIComponent(tag)}`)
      );
    }

    return { tags };
  }

  async downloadTranscript(videoId: string, language: string, format: string) {
    const path = `/videos/${videoId}/texttracks`;
    
    // Get available text tracks
    const tracks = await this.executeWithRateLimit(() =>
      this.makeApiCall("GET", path)
    );

    // Find the track for the requested language
    // Handle auto-generated transcripts (e.g., "en-x-autogen" for English auto-generated)
    let track = tracks.data?.find((t: any) => 
      t.language === language || t.language_code === language
    );

    // If not found and looking for "en", try auto-generated English
    if (!track && language === "en") {
      track = tracks.data?.find((t: any) => 
        t.language === "en-x-autogen" || t.language_code === "en-x-autogen"
      );
    }

    // If still not found, try any auto-generated track that starts with the language
    if (!track) {
      track = tracks.data?.find((t: any) => 
        t.language?.startsWith(language + "-") || t.language_code?.startsWith(language + "-")
      );
    }

    if (!track) {
      const availableLanguages = tracks.data?.map((t: any) => t.language || t.language_code).join(", ") || "none";
      throw new VimeoError(
        `No transcript available for language: ${language}. Available languages: ${availableLanguages}`,
        404
      );
    }

    // If the track has a direct download link, use it
    if (track.link) {
      const response = await fetch(track.link);
      if (!response.ok) {
        throw new VimeoError(
          `Failed to download transcript from direct link: ${response.statusText}`,
          response.status
        );
      }
      const trackContent = await response.text();
      
      // Convert format if needed (Vimeo typically provides WebVTT)
      if (format === "srt" && trackContent.includes("WEBVTT")) {
        return this.convertWebVTTToSRT(trackContent);
      }
      
      return trackContent;
    }

    // Fallback: Try to get content via API
    const trackContent = await this.executeWithRateLimit(() =>
      this.makeApiCall("GET", track.uri)
    );

    // Convert format if needed (Vimeo typically provides WebVTT)
    if (format === "srt" && trackContent.includes("WEBVTT")) {
      return this.convertWebVTTToSRT(trackContent);
    }

    return trackContent;
  }

  async uploadTranscript(
    videoId: string,
    content: string,
    language: string,
    format: string
  ) {
    const path = `/videos/${videoId}/texttracks`;

    // Convert SRT to WebVTT if needed (Vimeo prefers WebVTT)
    let processedContent = content;
    if (format === "srt" && !content.includes("WEBVTT")) {
      processedContent = this.convertSRTToWebVTT(content);
    }

    const params = {
      type: "captions",
      language: language,
      name: `Captions (${language})`,
    };

    // Create the text track
    const track = await this.executeWithRateLimit(() =>
      this.makeApiCall("POST", path, params)
    );

    // Upload the content to the provided link
    if (track.link) {
      await this.executeWithRateLimit(async () => {
        const uploadResponse = await fetch(track.link, {
          method: "PUT",
          body: processedContent,
          headers: {
            "Content-Type": "text/vtt",
          },
        });

        if (!uploadResponse.ok) {
          throw new VimeoError(
            `Failed to upload transcript: ${uploadResponse.statusText}`,
            uploadResponse.status
          );
        }
      });
    }

    return track;
  }

  async getVideoStats(videoId: string) {
    const path = `/videos/${videoId}/stats`;
    return this.executeWithRateLimit(() =>
      this.makeApiCall("GET", path)
    );
  }

  // File management and content analysis methods
  private generateTitleSlug(title: string): string {
    return title
      .toLowerCase()
      .replace(/[^a-z0-9\s-]/g, '')
      .replace(/\s+/g, '-')
      .replace(/-+/g, '-')
      .substring(0, 50)
      .replace(/^-+|-+$/g, '');
  }

  private ensureDirectoryExists(dirPath: string): void {
    if (!fs.existsSync(dirPath)) {
      fs.mkdirSync(dirPath, { recursive: true });
    }
  }

  private analyzeTranscriptContent(transcript: string): ContentAnalysis {
    const textContent = this.extractTextFromTranscript(transcript);
    const words = textContent.toLowerCase().split(/\s+/);
    
    // Keyword analysis
    const codingKeywords = ['code', 'coding', 'programming', 'function', 'variable', 'javascript', 'react', 'api', 'database', 'bug', 'debugging', 'development', 'software', 'algorithm', 'framework'];
    const motivationKeywords = ['motivation', 'journey', 'goal', 'success', 'challenge', 'progress', 'mindset', 'growth', 'inspiration', 'hustle', 'determination', 'achievement'];
    
    let codingScore = 0;
    let motivationScore = 0;
    const keywords: { [key: string]: number } = {};
    
    // Count keywords
    words.forEach(word => {
      if (codingKeywords.includes(word)) codingScore++;
      if (motivationKeywords.includes(word)) motivationScore++;
      keywords[word] = (keywords[word] || 0) + 1;
    });
    
    // Determine content type
    let contentType = 'Both';
    if (codingScore > motivationScore && codingScore > 3) {
      contentType = 'Coding';
    } else if (motivationScore > codingScore && motivationScore > 2) {
      contentType = 'Motivation';
    }
    
    // Extract key topics
    const keyTopics = Object.entries(keywords)
      .filter(([word, count]) => count > 2 && word.length > 3)
      .sort(([,a], [,b]) => b - a)
      .slice(0, 5)
      .map(([word]) => word);
    
    // Determine session type and other attributes
    const sessionType = textContent.includes('hour') || textContent.includes('time') ? 'Sprint Session' : 'Regular Session';
    const sentiment = codingScore > 5 ? 'Focused' : motivationScore > 3 ? 'Energetic' : 'Balanced';
    const technicalLevel = codingScore > 8 ? 'Advanced' : codingScore > 4 ? 'Intermediate' : 'Beginner';
    
    return {
      contentType,
      codingScore,
      motivationScore,
      keyTopics,
      keywords: Object.fromEntries(Object.entries(keywords).slice(0, 10)),
      sentiment,
      sessionType,
      technicalLevel
    };
  }

  private extractTextFromTranscript(transcript: string): string {
    return transcript
      .split('\n')
      .filter(line => 
        line.trim() && 
        !line.includes('WEBVTT') && 
        !line.includes('-->') && 
        !/^\d+$/.test(line.trim())
      )
      .join(' ')
      .replace(/\s+/g, ' ')
      .trim();
  }

  private generateMarkdownSuggestions(videoDetails: any, analysis: ContentAnalysis, transcript: string): string {
    const videoDate = new Date(videoDetails.created_time).toISOString().split('T')[0];
    const dayNumber = new Date(videoDetails.created_time).getDate();
    const duration = `${Math.floor(videoDetails.duration / 60)}:${(videoDetails.duration % 60).toString().padStart(2, '0')}`;
    
    // Generate title suggestions
    const titleSuggestions = this.generateTitleSuggestions(dayNumber, analysis);
    
    // Generate description
    const description = this.generateDescription(dayNumber, analysis, videoDetails);
    
    // Generate tags
    const tags = this.generateTags(analysis);
    
    const markdown = `# Video Content Analysis & Suggestions

## 📹 Video Details
- **Video ID**: ${videoDetails.uri.split('/').pop()}
- **Original Title**: ${videoDetails.name}
- **Duration**: ${duration}
- **Upload Date**: ${videoDate}
- **View Count**: ${videoDetails.stats?.plays || 0}
- **Privacy**: ${videoDetails.privacy?.view || 'Unknown'}

## 📊 Content Analysis
- **Content Type**: ${analysis.contentType} (Coding: ${analysis.codingScore}, Motivation: ${analysis.motivationScore})
- **Key Topics**: ${analysis.keyTopics.join(', ')}
- **Top Keywords**: ${Object.entries(analysis.keywords).slice(0, 5).map(([word, count]) => `${word} (${count}x)`).join(', ')}
- **Sentiment**: ${analysis.sentiment}
- **Technical Level**: ${analysis.technicalLevel}
- **Session Type**: ${analysis.sessionType}

## 🎯 Title Suggestions

### Primary Recommendations
${titleSuggestions.slice(0, 3).map((title, i) => `${i + 1}. **${title}**`).join('\n')}

### Alternative Options
${titleSuggestions.slice(3, 6).map((title, i) => `${i + 4}. ${title}`).join('\n')}

## 📝 Description Suggestion

${description}

## 🏷️ Tag Suggestions

### Primary Tags
${tags.primary.map(tag => `- ${tag}`).join('\n')}

### Secondary Tags
${tags.secondary.map(tag => `- ${tag}`).join('\n')}

### Niche Tags
${tags.niche.map(tag => `- ${tag}`).join('\n')}

## 📈 Optimization Notes
- **Best posting time**: Based on your audience, consider 9 AM or 7 PM
- **Thumbnail suggestion**: Split screen - you working + "DAY ${dayNumber}" text overlay
- **Engagement hook**: Lead with the ${analysis.sessionType.toLowerCase()} angle
- **Series continuity**: Reference previous days' progress
- **Call-to-action**: Ask about viewers' ${analysis.contentType.toLowerCase()} strategies

## 🔍 Content Insights
- **Strength**: Authentic, ${analysis.sessionType.toLowerCase()} resonate with entrepreneurs
- **Pattern**: This is part of your ${analysis.contentType.toLowerCase()}-heavy content
- **Suggestion**: Consider adding quick wins/tips section for better retention

## 📊 Performance Predictions
- **Estimated engagement**: ${analysis.codingScore > 5 ? 'High' : 'Medium'} (${analysis.contentType.toLowerCase()} content + authentic format)
- **Target audience**: Entrepreneurs, developers, productivity enthusiasts
- **Shareability**: ${analysis.motivationScore > 3 ? 'High' : 'Medium'} (relatable ${analysis.contentType.toLowerCase()} content)
- **Series value**: High (consistent daily format builds habit viewing)

---
*Generated on: ${new Date().toISOString().split('T')[0]} at ${new Date().toLocaleTimeString()} PST*
*Transcript length: ${transcript.length.toLocaleString()} characters*
*Analysis confidence: ${Math.min(95, 70 + analysis.codingScore + analysis.motivationScore)}%*
`;

    return markdown;
  }

  private generateTitleSuggestions(dayNumber: number, analysis: ContentAnalysis): string[] {
    const suggestions = [];
    
    // Determine content type for format: Motivation/Coding/Both(MoCo)
    let formatType: string;
    if (analysis.codingScore >= 5 && analysis.motivationScore >= 3) {
      formatType = "Both(MoCo)";
    } else if (analysis.codingScore >= analysis.motivationScore) {
      formatType = "Coding";
    } else {
      formatType = "Motivation";
    }
    
    // Extract key topics for personalized suggestions
    const hasMarketing = analysis.keyTopics.some(topic => topic.includes('market'));
    const hasTime = analysis.keyTopics.some(topic => topic.includes('hour') || topic.includes('time'));
    const hasProject = analysis.keyTopics.some(topic => topic.includes('proco') || topic.includes('project'));
    
    // Generate titles with standardized format: Day [#] - [1M Journey] - [Motivation/Coding/Both(MoCo)] - [Title]
    if (hasMarketing && hasProject) {
      suggestions.push(`Day ${dayNumber} - 1M Journey - ${formatType} - ProX Marketing Sprint Session`);
      suggestions.push(`Day ${dayNumber} - 1M Journey - ${formatType} - Building the Marketing Engine`);
    }
    
    if (hasTime) {
      suggestions.push(`Day ${dayNumber} - 1M Journey - ${formatType} - Racing Against Time Challenge`);
      suggestions.push(`Day ${dayNumber} - 1M Journey - ${formatType} - One Hour Power Session`);
    }
    
    if (analysis.sessionType === 'Sprint Session') {
      suggestions.push(`Day ${dayNumber} - 1M Journey - ${formatType} - Focused Sprint Session`);
      suggestions.push(`Day ${dayNumber} - 1M Journey - ${formatType} - High-Intensity Work Block`);
    }
    
    // Enhanced generic suggestions with viral hooks
    suggestions.push(`Day ${dayNumber} - 1M Journey - ${formatType} - Daily Progress Update`);
    suggestions.push(`Day ${dayNumber} - 1M Journey - ${formatType} - Building the Dream`);
    suggestions.push(`Day ${dayNumber} - 1M Journey - ${formatType} - Hustle & Flow Session`);
    suggestions.push(`Day ${dayNumber} - 1M Journey - ${formatType} - Real Work, Real Progress`);
    suggestions.push(`Day ${dayNumber} - 1M Journey - ${formatType} - The Grind Continues`);
    suggestions.push(`Day ${dayNumber} - 1M Journey - ${formatType} - Making It Happen`);
    
    return suggestions.slice(0, 6);
  }

  private generateDescription(dayNumber: number, analysis: ContentAnalysis, videoDetails: any): string {
    const duration = Math.floor(videoDetails.duration / 60);
    
    let topicsSection = '• Progress updates and insights\n• Real-time problem solving\n• Building towards the 1M goal';
    
    if (analysis.keyTopics.includes('marketing')) {
      topicsSection = '• Marketing strategy development\n• Strategic planning and execution\n' + topicsSection;
    }
    
    if (analysis.keyTopics.some(topic => topic.includes('proco'))) {
      topicsSection = '• Proco Pro XI project development\n' + topicsSection;
    }
    
    return `🚀 **Day ${dayNumber} of my journey to 1 million!**

📝 In this ${analysis.contentType.toLowerCase()} session, I dive into:
${topicsSection}

**Key Takeaways:**
- How to maintain ${analysis.sentiment.toLowerCase()} execution under pressure
- ${analysis.contentType} strategies for entrepreneurs
- Real-world problem-solving techniques

💪 Every day brings new challenges on this journey! Follow along as I document the real, unfiltered process of building something meaningful.

**Journey Stats:**
- 🗓️ Day: ${dayNumber}
- ⏱️ Session: ${duration} minutes
- 🎯 Focus: ${analysis.keyTopics.slice(0, 2).join(' & ')}
- 🔥 Energy: ${analysis.sentiment}

#Journey1M #Entrepreneur #${analysis.contentType} ${analysis.keyTopics.map(topic => `#${topic.charAt(0).toUpperCase() + topic.slice(1)}`).slice(0, 3).join(' ')}

**What's your biggest challenge when ${analysis.contentType === 'Coding' ? 'developing' : 'staying motivated'}?** Drop a comment below! 👇

---
*This is part of my daily documentation series as I work towards 1 million in revenue/impact. Raw, real, unfiltered.*`;
  }

  private generateTags(analysis: ContentAnalysis): { primary: string[]; secondary: string[]; niche: string[] } {
    return {
      primary: ['Journey1M', 'Entrepreneur', analysis.contentType, 'Progress', 'RealTalk'],
      secondary: ['Productivity', 'Startup', 'TechEntrepreneur', 'WorkSession', 'DailyProgress'],
      niche: [
        ...analysis.keyTopics.slice(0, 3).map(topic => topic.charAt(0).toUpperCase() + topic.slice(1)),
        analysis.sessionType.replace(' ', ''),
        `${analysis.contentType}Session`
      ]
    };
  }

  async downloadTranscriptToFile(videoId: string, language: string = 'en', format: string = 'webvtt', basePath: string = './downloads/vimeo'): Promise<string> {
    // Get video details and transcript
    const videoDetails = await this.getVideoDetails(videoId);
    const transcript = await this.downloadTranscript(videoId, language, format);
    
    // Create date-based directory structure
    const videoDate = new Date(videoDetails.created_time).toISOString().split('T')[0];
    const dateDir = path.join(basePath, videoDate);
    const transcriptsDir = path.join(dateDir, 'transcripts');
    const suggestionsDir = path.join(dateDir, 'suggestions');
    
    this.ensureDirectoryExists(transcriptsDir);
    this.ensureDirectoryExists(suggestionsDir);
    
    // Generate smart filename
    const titleSlug = this.generateTitleSlug(videoDetails.name);
    const filename = `${videoDate}_${titleSlug}_${videoId}.${format === 'webvtt' ? 'vtt' : 'srt'}`;
    const transcriptPath = path.join(transcriptsDir, filename);
    
    // Save transcript
    fs.writeFileSync(transcriptPath, transcript);
    
    return transcriptPath;
  }

  async generateContentAnalysisFile(videoId: string, basePath: string = './downloads/vimeo'): Promise<string> {
    // Get video details and transcript
    const videoDetails = await this.getVideoDetails(videoId);
    const transcript = await this.downloadTranscript(videoId, 'en', 'webvtt');
    
    // Analyze content
    const analysis = this.analyzeTranscriptContent(transcript);
    
    // Create date-based directory structure
    const videoDate = new Date(videoDetails.created_time).toISOString().split('T')[0];
    const dateDir = path.join(basePath, videoDate);
    const suggestionsDir = path.join(dateDir, 'suggestions');
    
    this.ensureDirectoryExists(suggestionsDir);
    
    // Generate markdown
    const markdown = this.generateMarkdownSuggestions(videoDetails, analysis, transcript);
    
    // Generate filename
    const titleSlug = this.generateTitleSlug(videoDetails.name);
    const filename = `${videoDate}_${titleSlug}_suggestions.md`;
    const suggestionsPath = path.join(suggestionsDir, filename);
    
    // Save markdown file
    fs.writeFileSync(suggestionsPath, markdown);
    
    return suggestionsPath;
  }

  private convertWebVTTToSRT(webvtt: string): string {
    // Basic WebVTT to SRT conversion
    let srt = webvtt
      .replace(/WEBVTT\r?\n\r?\n/, "")
      .replace(/(\d{2}:\d{2}:\d{2})\.(\d{3})/g, "$1,$2")
      .trim();

    // Add sequence numbers if not present
    const lines = srt.split("\n");
    const result: string[] = [];
    let sequenceNumber = 1;

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i].trim();
      if (line && line.includes("-->")) {
        result.push(String(sequenceNumber++));
        result.push(line);
      } else if (line) {
        result.push(line);
      } else if (result[result.length - 1] !== "") {
        result.push("");
      }
    }

    return result.join("\n");
  }

  private convertSRTToWebVTT(srt: string): string {
    // Basic SRT to WebVTT conversion
    const webvtt = srt
      .replace(/(\d{2}:\d{2}:\d{2}),(\d{3})/g, "$1.$2")
      .replace(/^\d+\r?\n/gm, "");

    return `WEBVTT\n\n${webvtt}`;
  }
}