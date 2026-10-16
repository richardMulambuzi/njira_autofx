/**
 * Common API request properties shared across various API calls
 */
declare interface BaseRequest {
  /**
   * Unique identifier for the request
   */
  req_id?: number;

  /**
   * Echo back the original request for identification
   */
  passthrough?: any;
}

/**
 * Common API response structure
 */
declare interface BaseResponse {
  /**
   * Echo of the request identifier if provided
   */
  req_id?: number;

  /**
   * Request message type
   */
  msg_type: string;

  /**
   * Error object if request failed
   */
  error?: {
    code: string;
    message: string;
  };

  /**
   * Echo back of the original request for identification
   */
  passthrough?: any;
}
