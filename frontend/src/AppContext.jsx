import { createContext, useContext, useReducer } from 'react'
import { createInitialState, saveState } from './state.js'

export const AppContext = createContext(null)

function reducer(state, action) {
  const next = { ...state, ...action }
  // auto-persist certain keys
  saveState(next)
  return next
}

export function AppProvider({ children }) {
  const [state, dispatch] = useReducer(reducer, null, createInitialState)
  const update = (patch) => dispatch(patch)
  return (
    <AppContext.Provider value={{ state, update }}>
      {children}
    </AppContext.Provider>
  )
}

export function useApp() {
  return useContext(AppContext)
}
