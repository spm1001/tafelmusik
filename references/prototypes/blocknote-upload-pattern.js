import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { writeFile, mkdir } from 'fs/promises'
import { join } from 'path'
import { randomUUID } from 'crypto'

export default defineConfig({
  plugins: [
    react(),
    {
      name: 'upload-middleware',
      configureServer(server) {
        const uploadDir = join(process.cwd(), 'uploads')

        // Serve uploaded files
        server.middlewares.use('/uploads', async (req, res, next) => {
          if (req.method === 'GET') {
            const { createReadStream, existsSync } = await import('fs')
            const filePath = join(uploadDir, req.url.replace(/^\//, ''))
            if (existsSync(filePath)) {
              const ext = filePath.split('.').pop().toLowerCase()
              const mimeTypes = {
                png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg',
                gif: 'image/gif', webp: 'image/webp', svg: 'image/svg+xml',
                pdf: 'application/pdf',
              }
              res.setHeader('Content-Type', mimeTypes[ext] || 'application/octet-stream')
              createReadStream(filePath).pipe(res)
            } else {
              res.statusCode = 404
              res.end('Not found')
            }
          } else {
            next()
          }
        })

        // Handle upload
        server.middlewares.use('/api/upload', async (req, res) => {
          if (req.method !== 'POST') {
            res.statusCode = 405
            res.end('Method not allowed')
            return
          }

          try {
            const chunks = []
            for await (const chunk of req) chunks.push(chunk)
            const body = Buffer.concat(chunks)

            // Parse multipart boundary
            const contentType = req.headers['content-type'] || ''
            const boundaryMatch = contentType.match(/boundary=(.+)/)
            if (!boundaryMatch) {
              res.statusCode = 400
              res.end(JSON.stringify({ error: 'No boundary in content-type' }))
              return
            }

            const boundary = boundaryMatch[1]
            const parts = body.toString('binary').split(`--${boundary}`)

            for (const part of parts) {
              const headerEnd = part.indexOf('\r\n\r\n')
              if (headerEnd === -1) continue

              const headers = part.substring(0, headerEnd)
              const filenameMatch = headers.match(/filename="([^"]+)"/)
              if (!filenameMatch) continue

              const originalName = filenameMatch[1]
              const ext = originalName.split('.').pop()
              const id = randomUUID().slice(0, 12)
              const safeName = `${id}.${ext}`

              const fileData = part.substring(headerEnd + 4, part.length - 2)
              await mkdir(uploadDir, { recursive: true })
              await writeFile(
                join(uploadDir, safeName),
                Buffer.from(fileData, 'binary')
              )

              const url = `/uploads/${safeName}`
              console.log(`[upload] ${originalName} → ${safeName} (${fileData.length} bytes)`)

              res.setHeader('Content-Type', 'application/json')
              res.end(JSON.stringify({ url, filename: originalName, id }))
              return
            }

            res.statusCode = 400
            res.end(JSON.stringify({ error: 'No file found in upload' }))
          } catch (err) {
            console.error('[upload] error:', err)
            res.statusCode = 500
            res.end(JSON.stringify({ error: err.message }))
          }
        })
      }
    }
  ],
})
