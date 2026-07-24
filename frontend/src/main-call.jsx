import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import CallApp from './CallApp.jsx'

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <CallApp />
  </StrictMode>,
)
