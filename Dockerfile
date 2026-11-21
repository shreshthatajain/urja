FROM node:22-slim
WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev --no-fund --no-audit
COPY settings.js flows.json ./
ENV NODE_ENV=production
EXPOSE 10000
CMD ["npm", "start"]
