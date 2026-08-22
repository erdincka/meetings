import axios, { AxiosError, AxiosInstance } from "axios"
import { toast } from "sonner"

export interface APIResponse<T = unknown> {
  status: "success" | "error"
  data?: T
  message?: string
  meta?: Record<string, unknown>
}

type QueryParams = Record<string, string | number | boolean | undefined>

class APIClient {
  private client: AxiosInstance

  constructor(baseURL: string) {
    this.client = axios.create({
      baseURL,
      timeout: 30000,
      headers: { "Content-Type": "application/json" },
    })

    this.client.interceptors.response.use(
      (response) => response,
      (error: AxiosError) => {
        const body = error.response?.data as { message?: string; detail?: string } | undefined
        const message = body?.message ?? body?.detail ?? error.message
        const status = error.response?.status

        if (status === 401) {
          toast.error("Session expired", { description: "Please sign in again." })
        } else if (status === 403) {
          toast.error("Access denied", { description: message })
        } else if (status === 404) {
          console.warn("Resource not found:", error.config?.url)
        } else if (status === 422) {
          toast.error("Invalid request", { description: message })
        } else if (status && status >= 500) {
          toast.error("Backend error", { description: message, duration: 5000 })
        } else if (error.code === "ECONNABORTED") {
          toast.warning("Request timed out", { description: "The backend did not respond in time." })
        } else {
          toast.error("Request failed", { description: message })
        }

        console.error("API error:", error.response?.data ?? error.message)
        return Promise.reject(error)
      }
    )
  }

  /**
   * Unwrap the APIResponse envelope, treating status === "error" as a failure.
   *
   * Only get() used to do this. post/put/patch/delete returned
   * `response.data.data` unconditionally, so a backend error envelope -- which
   * several routes return with HTTP 200 -- resolved as a *successful* mutation
   * with `data === undefined`, and the UI showed a success toast.
   */
  private unwrap<T>(payload: APIResponse<T>): T {
    if (payload.status === "error") {
      throw new Error(payload.message ?? "Request failed")
    }
    return payload.data as T
  }

  async get<T>(url: string, params?: QueryParams): Promise<T> {
    const response = await this.client.get<APIResponse<T>>(url, { params })
    return this.unwrap(response.data)
  }

  async post<T>(url: string, data?: unknown): Promise<T> {
    const response = await this.client.post<APIResponse<T>>(url, data)
    return this.unwrap(response.data)
  }

  async put<T>(url: string, data?: unknown): Promise<T> {
    const response = await this.client.put<APIResponse<T>>(url, data)
    return this.unwrap(response.data)
  }

  async patch<T>(url: string, data?: unknown): Promise<T> {
    const response = await this.client.patch<APIResponse<T>>(url, data)
    return this.unwrap(response.data)
  }

  async delete<T>(url: string): Promise<T> {
    const response = await this.client.delete<APIResponse<T>>(url)
    return this.unwrap(response.data)
  }
}

export const apiClient = new APIClient(process.env.NEXT_PUBLIC_API_URL || "/api/v1")
